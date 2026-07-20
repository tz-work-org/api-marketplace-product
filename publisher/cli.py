"""Command line entry point.

This is the only module that decides what a failure *means*. Core modules raise;
this one turns an outcome into an exit code. A Lambda handler will turn the same
outcome into a returned value (ADR-0001, rule 1).

Exit codes are deliberately non-standard (§A.9) so CI can tell "nothing to do"
apart from "would change things":

    0  converged / valid
    1  error
    2  changes pending

`validate`, `plan` and `apply` exist so far. `publish` arrives at §A.15 step 8.
This module also hosts the actual-state loader (§A.6 step 2): assembly of
portal → products → default section → entries → page bodies is orchestration,
and `cli` is the one module allowed to know every other one (§A.12).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path

from .executor import ExecutorError, apply
from .manifest import ManifestError, load_all_products
from .models import Operation, Product, TocEntry
from .portal_client import PortalClient, PortalError
from .reconciler import ReconcileError, enforce_max_deletes, reconcile

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES_PENDING = 2

DEFAULT_PRODUCTS_ROOT = Path("products")
DEFAULT_MAX_DELETES = 3  # §A.8


class ConfigurationError(ValueError):
    """A required environment variable is missing (§A.11)."""


def configuration_from_environment() -> tuple[str, str]:
    """Read the two required settings — the API key and the portal subdomain.

    The only place in the publisher that touches `os.environ` (ADR-0001,
    rule 2), so every runner — CLI, Jenkins, Lambda — configures it the same
    way and nothing else can grow a hidden environment dependency.
    """
    api_key = os.environ.get("SWAGGERHUB_API_KEY", "")
    subdomain = os.environ.get("PORTAL_SUBDOMAIN", "")

    missing = [
        name
        for name, value in (
            ("SWAGGERHUB_API_KEY", api_key),
            ("PORTAL_SUBDOMAIN", subdomain),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"Missing environment variable(s): {', '.join(missing)}. See .env.example."
        )
    return api_key, subdomain


# --- actual state (§A.6 step 2) --------------------------------------------


def load_actual_state(
    client: PortalClient, portal_id: str, slugs_to_detail: set[str]
) -> list[Product]:
    """Assemble what the portal currently holds.

    Every product is listed, so product-level orphans stay visible. The full
    tree — default section, entries, page bodies — is fetched only for
    products the repository also declares: they are the only ones the diff
    compares entry by entry, and each level is another round of calls.

    Takes the portal id rather than the subdomain: the caller resolves it once
    and shares it with the executor, which needs it to create products.
    """
    products = []
    for product in client.list_products(portal_id):
        if product.slug in slugs_to_detail:
            section_id = client.get_default_section_id(product.id)
            entries = _with_page_bodies(
                client, client.list_table_of_contents(section_id)
            )
            product = dataclasses.replace(product, entries=entries)
        products.append(product)
    return products


def _with_page_bodies(
    client: PortalClient, entries: list[TocEntry]
) -> tuple[TocEntry, ...]:
    """Fetch the text behind each documentation entry.

    The portal returns a page entry carrying only a document id; the
    reconciler compares bodies, so they must be filled in before the diff
    runs. API references have no document and pass through untouched.
    """
    filled = []
    for entry in entries:
        if entry.document is not None and entry.document.id:
            entry = dataclasses.replace(
                entry, document=client.get_document(entry.document.id)
            )
        filled.append(entry)
    return tuple(filled)


# --- output formatting ------------------------------------------------------


def describe_product(product: Product) -> list[str]:
    """Render one product as human-readable lines.

    Formatting lives here rather than in the loader, so the logic stays free of
    presentation and can be reused by a handler that returns JSON instead
    (ADR-0001, rule 3).
    """
    visibility = "public" if product.public else "internal"
    if product.hidden:
        visibility += ", hidden"

    lines = [
        f"{product.name}  ({product.slug})",
        f"  {product.description}",
        f"  {visibility} | owner {product.owner.name} <{product.owner.email}> "
        f"@{product.owner.github_handle}",
        f"  {len(product.entries)} entries, {len(product.api_references)} API references",
    ]

    for entry in product.entries:
        parent = f" under {entry.parent_slug}" if entry.parent_slug else ""
        lines.append(
            f"    {entry.order}. {entry.title}  [{entry.content_type}] "
            f"{entry.slug}{parent}"
        )
    return lines


def describe_operation(operation: Operation) -> str:
    """One plan line per operation — `CREATE toc-entry claims/getting-started`.

    Updates name the fields that differ, because a bare UPDATE forces the
    reader to diff by hand. Orphans say what would (not) happen to them, so an
    accidental slug edit — which plans as a create plus an orphan (§A.7) — is
    visible for exactly what it is.
    """
    line = f"{operation.verb.upper()} {operation.resource} {operation.path}"
    if operation.changes:
        line += f" ({', '.join(operation.changes)})"
    if operation.verb == "orphan":
        if operation.resource == "product":
            line += " — portal only; product deletion is out of scope, retire with hidden"
        else:
            line += " — portal only; --prune would delete it"
    return line


# --- commands ---------------------------------------------------------------


def run_validate(products_root: Path) -> int:
    """Load and validate every manifest, printing what was found.

    Printing the parsed result rather than only 'OK' is deliberate: it shows the
    author what the tool *understood*, which is where a manifest mistake becomes
    visible.
    """
    products = load_all_products(products_root)

    for product in products:
        for line in describe_product(product):
            print(line)
        print()

    print(f"# {len(products)} product(s) valid")
    return EXIT_OK


def run_plan(
    products_root: Path,
    client: PortalClient,
    subdomain: str,
    prune: bool = False,
    max_deletes: int = DEFAULT_MAX_DELETES,
) -> int:
    """Print the diff between the repository and the portal. Changes nothing.

    A converged plan prints nothing at all (§A.14 #3). Exit code 2 means an
    apply would act; orphans alone do not count — with pruning off, nothing
    would touch them — but they are always shown (§A.8). The client arrives as
    a parameter so tests can hand in a fake and stay off the network.
    """
    desired = load_all_products(products_root)
    portal_id = client.get_portal_id(subdomain)
    actual = load_actual_state(client, portal_id, {p.slug for p in desired})

    operations = reconcile(desired, actual, prune=prune)
    enforce_max_deletes(operations, max_deletes)

    if not operations:
        return EXIT_OK

    for operation in operations:
        print(describe_operation(operation))

    actionable = [op for op in operations if op.verb != "orphan"]
    orphan_count = len(operations) - len(actionable)
    print(f"# {len(actionable)} change(s) pending, {orphan_count} orphan(s)")

    return EXIT_CHANGES_PENDING if actionable else EXIT_OK


def run_apply(products_root: Path, client: PortalClient, subdomain: str) -> int:
    """Make the portal match the repository, then report what was done.

    The same load-and-reconcile as `plan`, but the operations are executed
    instead of printed. Pruning is off (§A.15 step 7): creates and updates only,
    so deletions never arise — deletion is step 9, sequenced last because it is
    destructive. Orphans are shown but left untouched, exactly as `plan` shows
    them.

    Each operation is logged as it completes, so a run that fails part-way still
    shows what succeeded; there is no rollback and re-running converges (§A.6).
    A converged repository applies nothing and exits 0 (§A.9).
    """
    desired = load_all_products(products_root)
    portal_id = client.get_portal_id(subdomain)
    actual = load_actual_state(client, portal_id, {p.slug for p in desired})

    operations = reconcile(desired, actual, prune=False)
    actionable = [op for op in operations if op.verb != "orphan"]
    orphans = [op for op in operations if op.verb == "orphan"]

    apply(actionable, actual, client, portal_id, log=_print_operation)

    for operation in orphans:
        print(describe_operation(operation))
    print(
        f"# {len(actionable)} change(s) applied, "
        f"{len(orphans)} orphan(s) left untouched"
    )
    return EXIT_OK


def _print_operation(operation: Operation) -> None:
    """Log one applied operation — the same line `plan` would have shown."""
    print(describe_operation(operation))


# --- argument parsing and dispatch ------------------------------------------


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publisher",
        description="Reconcile product manifests against a Swagger Portal.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser(
        "validate", help="Load and validate every manifest, then print it."
    )
    validate.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_ROOT,
        help="Directory holding product folders (default: products).",
    )

    plan = subcommands.add_parser(
        "plan", help="Show what apply would change. Changes nothing."
    )
    plan.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_ROOT,
        help="Directory holding product folders (default: products).",
    )
    plan.add_argument(
        "--prune",
        action="store_true",
        help="Plan deletions for portal-only entries instead of listing them as orphans.",
    )
    plan.add_argument(
        "--max-deletes",
        type=int,
        default=DEFAULT_MAX_DELETES,
        help=f"Abort when the plan wants more than this many deletions "
        f"(default: {DEFAULT_MAX_DELETES}, §A.8).",
    )

    apply_command = subcommands.add_parser(
        "apply",
        help="Make the portal match the repository (creates and updates only).",
    )
    apply_command.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_ROOT,
        help="Directory holding product folders (default: products).",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)

    try:
        if arguments.command == "validate":
            return run_validate(arguments.products)
        if arguments.command == "plan":
            api_key, subdomain = configuration_from_environment()
            client = PortalClient(api_key)
            return run_plan(
                arguments.products,
                client,
                subdomain,
                prune=arguments.prune,
                max_deletes=arguments.max_deletes,
            )
        if arguments.command == "apply":
            api_key, subdomain = configuration_from_environment()
            client = PortalClient(api_key)
            return run_apply(arguments.products, client, subdomain)
    except (
        ManifestError,
        ReconcileError,
        ConfigurationError,
        PortalError,
        ExecutorError,
    ) as error:
        print(f"FAILED: {error}")
        return EXIT_ERROR

    return EXIT_ERROR
