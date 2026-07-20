"""Command line entry point.

This is the only module that decides what a failure *means*. Core modules raise;
this one turns an outcome into an exit code. A Lambda handler will turn the same
outcome into a returned value (ADR-0001, rule 1).

Exit codes are deliberately non-standard (§A.9) so CI can tell "nothing to do"
apart from "would change things":

    0  converged / valid
    1  error
    2  changes pending

`validate`, `plan`, `apply` and `publish` exist. Deletions (§A.15 step 9) are the
remaining destructive step. This module also hosts the actual-state loader (§A.6
step 2): assembly of portal → products → default section → entries → page bodies
is orchestration, and `cli` is the one module allowed to know every other one
(§A.12).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path

from .executor import ExecutorError, apply
from .manifest import ManifestError, load_all_products
from .models import (
    Operation,
    Product,
    TocEntry,
    UnpublishedChanges,
    ValidationMessage,
)
from .portal_client import PortalClient, PortalError
from .reconciler import (
    ReconcileError,
    enforce_max_deletes,
    products_emptied_by_prune,
    reconcile,
)

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

    for slug in products_emptied_by_prune(desired, operations):
        print(f"# WARNING: prune leaves product '{slug}' with no API references (§A.8)")

    actionable = [op for op in operations if op.verb != "orphan"]
    orphan_count = len(operations) - len(actionable)
    print(f"# {len(actionable)} change(s) pending, {orphan_count} orphan(s)")

    return EXIT_CHANGES_PENDING if actionable else EXIT_OK


def run_apply(
    products_root: Path,
    client: PortalClient,
    subdomain: str,
    prune: bool = False,
    max_deletes: int = DEFAULT_MAX_DELETES,
) -> int:
    """Make the portal match the repository, then report what was done.

    The same load-and-reconcile as `plan`, but the operations are executed
    instead of printed. Pruning is off by default (§A.8): creates and updates
    only, and portal-only entries are shown as orphans, left untouched — exactly
    as `plan` shows them. With `--prune` those entries become soft-deletes, the
    one destructive path, still bounded by `--max-deletes`: exceeding it aborts
    the whole run before anything executes, rather than deleting part-way. A
    prune that would strip a product's last API reference is warned about,
    loudly, before it runs.

    Each operation is logged as it completes, so a run that fails part-way still
    shows what succeeded; there is no rollback and re-running converges (§A.6).
    A converged repository applies nothing and exits 0 (§A.9).
    """
    desired = load_all_products(products_root)
    portal_id = client.get_portal_id(subdomain)
    actual = load_actual_state(client, portal_id, {p.slug for p in desired})

    operations = reconcile(desired, actual, prune=prune)
    enforce_max_deletes(operations, max_deletes)

    for slug in products_emptied_by_prune(desired, operations):
        print(f"# WARNING: prune leaves product '{slug}' with no API references (§A.8)")

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


def run_publish(
    products_root: Path, client: PortalClient, subdomain: str, preview: bool = False
) -> int:
    """Promote each product's draft content to the live consumer view.

    Publishing is product-scoped and separate from apply (§A.6): apply writes the
    draft, publish makes it visible. With `--preview` the portal validates the
    draft and the pending changes are shown, but nothing is published — exit 2
    when something is staged, mirroring `plan`. A validation error (an API
    reference whose URL will not resolve, say) is a failure, exit 1.

    A product declared in the repo but not yet in the portal is skipped: apply
    must create it before it can be published. A product with nothing staged is
    already published and left alone.
    """
    desired = load_all_products(products_root)
    portal_id = client.get_portal_id(subdomain)
    actual_by_slug = {
        product.slug: product for product in client.list_products(portal_id)
    }

    staged_total = 0
    error_total = 0
    published_total = 0

    for product in desired:
        match = actual_by_slug.get(product.slug)
        if match is None:
            print(f"# {product.slug}: not in portal — run apply first")
            continue

        changes = client.get_unpublished_changes(match.id)
        if changes.is_empty:
            print(f"# {product.slug}: already published, nothing to do")
            continue

        staged_total += len(changes.changes)
        print(f"{'PREVIEW' if preview else 'PUBLISH'} product {product.slug}")
        for line in describe_changes(product.slug, changes):
            print(line)

        for message in client.publish_product(match.id, preview=preview):
            print(describe_validation(message))
            if message.is_error:
                error_total += 1

        if not preview:
            published_total += 1

    if preview:
        print(
            f"# {staged_total} change(s) staged, {error_total} error(s) — "
            f"preview only, nothing published"
        )
    else:
        print(f"# {published_total} product(s) published, {error_total} error(s)")

    if error_total:
        return EXIT_ERROR
    if preview and staged_total:
        return EXIT_CHANGES_PENDING
    return EXIT_OK


def describe_changes(product_slug: str, changes: UnpublishedChanges) -> list[str]:
    """Render a product's staged changes, one indented line each.

    The portal's `path` ends in the entry slug, so the last segment reads back as
    the familiar `product/slug` (§A.7). `reordered` is a whole-product flag with
    no single entry behind it, so it prints on its own line.
    """
    lines = []
    for change in changes.changes:
        slug = change.path.rsplit("/", 1)[-1]
        lines.append(f"  {change.kind} {change.content_type} {product_slug}/{slug}")
    if changes.reordered:
        lines.append("  reordered navigation")
    return lines


def describe_validation(message: ValidationMessage) -> str:
    """One line for a publish validation message — `ERROR [INVALID_URL]: ...`."""
    code = f" [{message.error_code}]" if message.error_code else ""
    return f"  {message.level.upper()}{code}: {message.message}"


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
        help="Make the portal match the repository. Deletes only with --prune.",
    )
    apply_command.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_ROOT,
        help="Directory holding product folders (default: products).",
    )
    apply_command.add_argument(
        "--prune",
        action="store_true",
        help="Soft-delete portal-only entries instead of leaving them as orphans "
        "(§A.8). Off by default; deletes are the only destructive operation.",
    )
    apply_command.add_argument(
        "--max-deletes",
        type=int,
        default=DEFAULT_MAX_DELETES,
        help=f"Abort before executing anything if the plan wants more than this "
        f"many deletions (default: {DEFAULT_MAX_DELETES}, §A.8). Set 0 to forbid "
        f"deletes outright.",
    )

    publish = subcommands.add_parser(
        "publish", help="Promote applied draft content to the live view."
    )
    publish.add_argument(
        "--products",
        type=Path,
        default=DEFAULT_PRODUCTS_ROOT,
        help="Directory holding product folders (default: products).",
    )
    publish.add_argument(
        "--preview",
        action="store_true",
        help="Validate and show what would be published, without publishing.",
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
            return run_apply(
                arguments.products,
                client,
                subdomain,
                prune=arguments.prune,
                max_deletes=arguments.max_deletes,
            )
        if arguments.command == "publish":
            api_key, subdomain = configuration_from_environment()
            client = PortalClient(api_key)
            return run_publish(
                arguments.products, client, subdomain, preview=arguments.preview
            )
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
