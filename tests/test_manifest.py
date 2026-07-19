"""Tests for manifest loading and validation (§A.6 step 1)."""

from __future__ import annotations

import copy

import pytest
from conftest import VALID_MANIFEST, manifest_with, write_product

from publisher.manifest import ManifestError, load_all_products


# --- the happy path -------------------------------------------------------


def test_loads_a_valid_manifest(products_root):
    write_product(products_root, "Claims", VALID_MANIFEST)

    products = load_all_products(products_root)

    assert len(products) == 1
    assert products[0].slug == "claims"
    assert len(products[0].entries) == 2
    assert len(products[0].api_references) == 1


def test_product_name_comes_from_the_directory_not_the_manifest(products_root):
    """§A.4 makes the folder name the product name, so the two cannot disagree."""
    write_product(products_root, "Policy Admin", VALID_MANIFEST)

    assert load_all_products(products_root)[0].name == "Policy Admin"


def test_markdown_body_is_read_from_disk(products_root):
    write_product(products_root, "Claims", VALID_MANIFEST)

    entry = load_all_products(products_root)[0].entries[0]

    assert entry.document is not None
    assert entry.document.content == "# Hello"


def test_entries_are_returned_in_manifest_order(products_root):
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["contentMetadata"][0]["order"] = 9
    write_product(products_root, "Claims", manifest)

    slugs = [entry.slug for entry in load_all_products(products_root)[0].entries]

    assert slugs == ["claims-intake-api", "getting-started"]


def test_the_same_slug_is_allowed_under_different_parents(products_root):
    """Slugs identify entries scoped to their parent, not globally (§A.7)."""
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["contentMetadata"].append(
        {
            "order": 3,
            "name": "Overview",
            "slug": "getting-started",
            "type": "apiUrl",
            "contentUrl": "https://api.swaggerhub.com/apis/org/x/1.0.0/swagger.json",
            "parent": "claims-intake-api",
        }
    )
    write_product(products_root, "Claims", manifest)

    assert len(load_all_products(products_root)[0].entries) == 3


# --- limits the portal itself enforces, found during §A.2 verification -----
#
# Each of these would otherwise surface as a 400 part-way through an apply,
# with some products already written. Catching them at load time is what makes
# the failure a red build rather than a half-updated portal.


@pytest.mark.parametrize(
    "field, value, expected_message",
    [
        ("slug", "a" * 23, "productMetadata/slug"),
        ("slug", "Claims", "productMetadata/slug"),
        ("slug", "ab", "productMetadata/slug"),
        ("description", "x" * 111, "productMetadata/description"),
    ],
    ids=["slug-too-long", "slug-uppercase", "slug-too-short", "description-too-long"],
)
def test_rejects_values_the_portal_will_not_accept(
    products_root, field, value, expected_message
):
    write_product(products_root, "Claims", manifest_with(**{field: value}))

    with pytest.raises(ManifestError, match=expected_message):
        load_all_products(products_root)


def test_rejects_product_name_longer_than_the_portal_allows(products_root):
    """The directory name is the product name, and the portal caps it at 40.

    (22 is the *slug* limit — a distinction the first spec version blurred.)
    """
    write_product(products_root, "A" * 41, VALID_MANIFEST)

    with pytest.raises(ManifestError, match="41 characters"):
        load_all_products(products_root)


def test_accepts_product_name_up_to_the_forty_character_limit(products_root):
    """A 40-character directory name fits — the previous 22 cap was too strict."""
    write_product(products_root, "A" * 40, VALID_MANIFEST)

    products = load_all_products(products_root)

    assert products[0].name == "A" * 40


# --- schema and internal consistency --------------------------------------


def test_rejects_validate_apis_flag(products_root):
    """§A.5 removes validateAPIs — governance is the API pipeline's job."""
    write_product(products_root, "Claims", manifest_with(validateAPIs=True))

    with pytest.raises(ManifestError, match="validateAPIs"):
        load_all_products(products_root)


def test_rejects_missing_owner(products_root):
    manifest = copy.deepcopy(VALID_MANIFEST)
    del manifest["productMetadata"]["owner"]
    write_product(products_root, "Claims", manifest)

    with pytest.raises(ManifestError, match="owner"):
        load_all_products(products_root)


def test_rejects_missing_markdown_file(products_root):
    """Missing files fail at plan time, before anything reaches the portal."""
    write_product(products_root, "Claims", VALID_MANIFEST, with_markdown=False)

    with pytest.raises(ManifestError, match="referenced file not found"):
        load_all_products(products_root)


def test_rejects_relative_url_for_an_api_reference(products_root):
    """Only SwaggerHub-hosted API references can be linked to a product (§A.3)."""
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["contentMetadata"][1]["contentUrl"] = "some/local/path.yaml"
    write_product(products_root, "Claims", manifest)

    with pytest.raises(ManifestError, match="not an absolute URL"):
        load_all_products(products_root)


def test_rejects_duplicate_entry_slugs_under_the_same_parent(products_root):
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["contentMetadata"][1]["slug"] = "getting-started"
    write_product(products_root, "Claims", manifest)

    with pytest.raises(ManifestError, match="duplicate slug"):
        load_all_products(products_root)


def test_rejects_parent_that_is_not_in_the_manifest(products_root):
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["contentMetadata"][1]["parent"] = "does-not-exist"
    write_product(products_root, "Claims", manifest)

    with pytest.raises(ManifestError, match="does-not-exist"):
        load_all_products(products_root)


def test_rejects_two_products_claiming_the_same_slug(products_root):
    write_product(products_root, "Claims", VALID_MANIFEST)
    write_product(products_root, "Claims Duplicate", VALID_MANIFEST)

    with pytest.raises(ManifestError, match="Duplicate product slug"):
        load_all_products(products_root)


def test_rejects_invalid_json(products_root):
    product_directory = products_root / "Claims"
    product_directory.mkdir()
    (product_directory / "manifest.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(ManifestError, match="invalid JSON"):
        load_all_products(products_root)


def test_error_message_names_the_file(products_root):
    """A reconcile touches many products; 'invalid slug' alone is useless."""
    write_product(products_root, "Claims", manifest_with(slug="Claims"))

    with pytest.raises(ManifestError, match="manifest.json"):
        load_all_products(products_root)
