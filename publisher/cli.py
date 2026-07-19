"""Command line entry point.

This is the only module that decides what a failure *means*. Core modules raise;
this one turns an outcome into an exit code. A Lambda handler will turn the same
outcome into a returned value (ADR-0001, rule 1).

Exit codes are deliberately non-standard (§A.9) so CI can tell "nothing to do"
apart from "would change things":

    0  converged / valid
    1  error
    2  changes pending

Only `validate` exists so far. `plan`, `apply` and `publish` arrive at §A.15
steps 6 to 8, once the reconciler exists.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .manifest import ManifestError, load_all_products
from .models import Product

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES_PENDING = 2

DEFAULT_PRODUCTS_ROOT = Path("products")


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

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)

    try:
        if arguments.command == "validate":
            return run_validate(arguments.products)
    except ManifestError as error:
        print(f"FAILED: {error}")
        return EXIT_ERROR

    return EXIT_ERROR
