"""Load, validate and parse product manifests from the repository.

Step 1 of the reconciliation algorithm (§A.6): read the manifest and referenced
files from disk, validate against the schema, and fail fast with a clear message
naming the file and the problem.

Every failure raises `ManifestError`. Nothing here exits the process — only the
entry point decides what a failure means, so the same loader serves the CLI and
the Lambda handler (ADR-0001, rule 1).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from .models import API_REFERENCE_TYPE, DOCUMENT_TYPES, Document, Owner, Product, TocEntry

MANIFEST_FILENAME = "manifest.json"
SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "manifest.schema.json"

# Portal API caps product names at 40 characters (the 22-character limit is the
# *slug*, enforced by the schema). §A.4 makes the directory name the product
# name, so an over-long directory name is unrepresentable and must fail here
# rather than as a 400 part-way through an apply. Verified against the official
# smartbear-public/swaggerhub-portal-api/0.8.0-beta spec.
MAX_PRODUCT_NAME_LENGTH = 40


class ManifestError(ValueError):
    """A manifest is missing, malformed, or internally inconsistent.

    The message always names the file it came from. A reconcile can touch many
    products, and 'invalid slug' on its own tells the reader nothing.
    """


def load_schema() -> dict:
    """Read the manifest JSON schema from disk."""
    if not SCHEMA_PATH.exists():
        raise ManifestError(f"Manifest schema not found at {SCHEMA_PATH}")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_product(product_directory: Path, schema: dict) -> Product:
    """Load one product directory into a Product.

    The directory name becomes the product name (§A.4). The manifest supplies
    everything else.
    """
    manifest_path = product_directory / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ManifestError(f"{product_directory}: no {MANIFEST_FILENAME}")

    raw = _read_json(manifest_path)
    _validate_against_schema(raw, schema, manifest_path)

    name = product_directory.name
    if len(name) > MAX_PRODUCT_NAME_LENGTH:
        raise ManifestError(
            f"{manifest_path}: product name '{name}' is {len(name)} characters; "
            f"the portal allows at most {MAX_PRODUCT_NAME_LENGTH}. "
            f"Rename the directory."
        )

    metadata = raw["productMetadata"]
    entries = _load_entries(raw["contentMetadata"], product_directory, manifest_path)

    return Product(
        name=name,
        slug=metadata["slug"],
        description=metadata["description"],
        owner=_load_owner(metadata["owner"]),
        entries=entries,
        public=metadata.get("public", False),
        hidden=metadata.get("hidden", False),
        auto_publish=metadata.get("autoPublish", False),
        logo=metadata.get("logo"),
        logo_dark=metadata.get("logoDark"),
        source_path=str(product_directory),
    )


def load_all_products(products_root: Path) -> list[Product]:
    """Load every product directory under `products/`.

    Sorted by slug so that output is stable between runs — an unstable plan is
    hard to review, and reviewing the plan is the whole point of having one.
    """
    if not products_root.exists():
        raise ManifestError(f"Products directory not found: {products_root}")

    schema = load_schema()
    products = [
        load_product(directory, schema)
        for directory in sorted(products_root.iterdir())
        if directory.is_dir()
    ]

    _reject_duplicate_product_slugs(products)
    return sorted(products, key=lambda product: product.slug)


# --- internals -----------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"{path}: invalid JSON — {error}") from error


def _validate_against_schema(raw: dict, schema: dict, manifest_path: Path) -> None:
    """Validate a manifest, reporting the offending field path.

    jsonschema's default message does not say *where* in the document the
    problem is, which on a manifest with a dozen entries is the only thing the
    reader needs.
    """
    try:
        jsonschema.validate(instance=raw, schema=schema)
    except jsonschema.ValidationError as error:
        location = "/".join(str(part) for part in error.absolute_path) or "(root)"
        raise ManifestError(f"{manifest_path}: at '{location}' — {error.message}") from error


def _load_owner(raw: dict) -> Owner:
    return Owner(
        name=raw["name"],
        email=raw["email"],
        github_handle=raw["githubHandle"],
    )


def _load_entries(
    raw_entries: list[dict], product_directory: Path, manifest_path: Path
) -> tuple[TocEntry, ...]:
    """Build table-of-contents entries, reading page bodies from disk.

    Entries are returned in manifest `order`. The integers themselves are not
    sent onward unchanged — the portal is 0-based and does not promise to
    preserve them — so this establishes sequence, not absolute position.
    """
    entries = [
        _load_entry(raw_entry, product_directory, manifest_path) for raw_entry in raw_entries
    ]

    _reject_duplicate_entry_slugs(entries, manifest_path)
    _reject_unknown_parents(entries, manifest_path)

    return tuple(sorted(entries, key=lambda entry: entry.order))


def _load_entry(raw_entry: dict, product_directory: Path, manifest_path: Path) -> TocEntry:
    content_type = raw_entry["type"]
    content_url = raw_entry["contentUrl"]

    document = None
    if content_type in DOCUMENT_TYPES:
        document = _load_document(content_url, product_directory, manifest_path)
    elif content_type == API_REFERENCE_TYPE:
        _require_absolute_url(content_url, raw_entry["slug"], manifest_path)

    return TocEntry(
        slug=raw_entry["slug"],
        title=raw_entry["name"],
        order=raw_entry["order"],
        content_type=content_type,
        content_url=content_url,
        parent_slug=raw_entry.get("parent"),
        document=document,
    )


def _load_document(content_url: str, product_directory: Path, manifest_path: Path) -> Document:
    """Read a page body from a repo-relative path.

    Read at load time rather than at apply time so that a missing file fails
    during `plan`, before anything has been written to the portal.
    """
    document_path = product_directory / content_url
    if not document_path.exists():
        raise ManifestError(f"{manifest_path}: referenced file not found — {document_path}")

    return Document(
        content=document_path.read_text(encoding="utf-8"),
        source_path=str(document_path),
    )


def _require_absolute_url(content_url: str, slug: str, manifest_path: Path) -> None:
    """Reject a relative path used as an API reference.

    Only SwaggerHub-hosted API references can be linked to a product (§A.3), so
    a repo-relative path here is always a mistake — and one that would otherwise
    surface as an obscure portal error.
    """
    if not content_url.startswith(("http://", "https://")):
        raise ManifestError(
            f"{manifest_path}: entry '{slug}' is an apiUrl but contentUrl "
            f"'{content_url}' is not an absolute URL."
        )


def _reject_duplicate_entry_slugs(entries: list[TocEntry], manifest_path: Path) -> None:
    """Reject two entries sharing a slug under the same parent.

    Slugs identify entries scoped to their parent (§A.7), so the same slug may
    legitimately appear under two different parents.
    """
    seen: set[tuple[str | None, str]] = set()
    for entry in entries:
        key = (entry.parent_slug, entry.slug)
        if key in seen:
            parent = entry.parent_slug or "(top level)"
            raise ManifestError(
                f"{manifest_path}: duplicate slug '{entry.slug}' under {parent}."
            )
        seen.add(key)


def _reject_unknown_parents(entries: list[TocEntry], manifest_path: Path) -> None:
    """Reject a `parent` that names no entry in this manifest."""
    known_slugs = {entry.slug for entry in entries}
    for entry in entries:
        if entry.parent_slug and entry.parent_slug not in known_slugs:
            raise ManifestError(
                f"{manifest_path}: entry '{entry.slug}' names parent "
                f"'{entry.parent_slug}', which is not in this manifest."
            )


def _reject_duplicate_product_slugs(products: list[Product]) -> None:
    """Reject two products claiming the same slug.

    Product slugs must be unique within a portal, and two directories claiming
    one would make the reconcile order decide which wins.
    """
    seen: dict[str, str] = {}
    for product in products:
        if product.slug in seen:
            raise ManifestError(
                f"Duplicate product slug '{product.slug}' in "
                f"{seen[product.slug]} and {product.source_path}."
            )
        seen[product.slug] = product.source_path
