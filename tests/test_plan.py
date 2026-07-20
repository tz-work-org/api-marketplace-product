"""Tests for `plan` (§A.15 step 6) — the diff made visible before anything writes.

`run_plan` is exercised with a fake portal client, never the real one, so the
whole command is provable offline (ADR-0001). The fake mirrors conftest's
VALID_MANIFEST so "unchanged" means genuinely identical on both sides.
"""

from __future__ import annotations

import copy

import pytest
from conftest import VALID_MANIFEST, manifest_with, write_product

from publisher.cli import (
    EXIT_CHANGES_PENDING,
    EXIT_ERROR,
    EXIT_OK,
    load_actual_state,
    main,
    run_plan,
)
from publisher.models import Document, Product, TocEntry
from publisher.reconciler import ReconcileError

DESCRIPTION = VALID_MANIFEST["productMetadata"]["description"]
INTAKE_URL = VALID_MANIFEST["contentMetadata"][1]["contentUrl"]


class FakePortal:
    """Stands in for PortalClient: the same four read methods, canned data.

    Plain duck typing, no shared base class (§A.12). How the real client turns
    wire payloads into models is test_portal_client.py's business; here only
    the models it hands back matter.
    """

    def __init__(self, products=(), entries=(), documents=None):
        self.products = list(products)
        self.entries = list(entries)
        self.documents = documents or {}
        self.detailed_product_ids: list[str] = []

    def get_portal_id(self, subdomain: str) -> str:
        return "portal-1"

    def list_products(self, portal_id: str) -> list[Product]:
        return list(self.products)

    def get_default_section_id(self, product_id: str) -> str:
        self.detailed_product_ids.append(product_id)
        return f"sec-{product_id}"

    def list_table_of_contents(self, section_id: str) -> list[TocEntry]:
        return list(self.entries)

    def get_document(self, document_id: str) -> Document:
        return self.documents[document_id]

    def add_orphan(self, slug: str, order: int) -> None:
        """Append a page entry the repo does not declare, registering its
        document so `get_document` can answer for it — the real portal returns
        a page entry carrying only a document id and fills the body on fetch."""
        entry = orphan_entry(slug, order)
        self.entries.append(entry)
        self.documents[entry.document.id] = entry.document


def portal_matching_the_valid_manifest(body: str = "# Hello") -> FakePortal:
    """A fake portal whose state mirrors VALID_MANIFEST exactly."""
    return FakePortal(
        products=[
            Product(name="Claims", slug="claims", description=DESCRIPTION, id="prod-1")
        ],
        entries=[
            TocEntry(
                slug="getting-started",
                title="Getting Started",
                order=1,
                content_type="markdown",
                content_url="",
                document=Document(content="", id="doc-1"),
                id="toc-1",
            ),
            TocEntry(
                slug="claims-intake-api",
                title="Claims Intake API",
                order=2,
                content_type="apiUrl",
                content_url=INTAKE_URL,
                id="toc-2",
            ),
        ],
        documents={"doc-1": Document(content=body, id="doc-1")},
    )


def orphan_entry(slug: str, order: int) -> TocEntry:
    return TocEntry(
        slug=slug,
        title=slug.title(),
        order=order,
        content_type="markdown",
        content_url="",
        document=Document(content="", id=f"doc-{slug}"),
        id=f"toc-{slug}",
    )


# --- the converged case (§A.14 #3) -----------------------------------------


def test_unchanged_repo_exits_zero_and_prints_nothing(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)

    exit_code = run_plan(products_root, portal_matching_the_valid_manifest(), "acme")

    assert exit_code == EXIT_OK
    assert capsys.readouterr().out == ""


# --- pending changes -------------------------------------------------------


def test_a_new_product_plans_creates_and_exits_two(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)

    exit_code = run_plan(products_root, FakePortal(), "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "CREATE product claims" in output
    assert "CREATE toc-entry claims/getting-started" in output
    assert "CREATE toc-entry claims/claims-intake-api" in output


def test_a_changed_description_is_one_update_naming_the_field(products_root, capsys):
    write_product(
        products_root, "Claims", manifest_with(description="Same product, new words.")
    )

    exit_code = run_plan(products_root, portal_matching_the_valid_manifest(), "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "UPDATE product claims (description)" in output


def test_changed_page_text_is_a_document_update(products_root, capsys):
    """The repo says '# Hello'; the portal still holds older text."""
    write_product(products_root, "Claims", VALID_MANIFEST)

    exit_code = run_plan(
        products_root, portal_matching_the_valid_manifest(body="# Old words"), "acme"
    )

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "UPDATE document claims/getting-started" in output


# --- orphans and prune (§A.14 #8, §A.8) ------------------------------------


def test_an_orphan_is_shown_but_the_plan_still_converges(products_root, capsys):
    """With prune off nothing will touch the orphan, so apply would change
    nothing and the exit code says converged — but the plan must show it."""
    write_product(products_root, "Claims", VALID_MANIFEST)
    portal = portal_matching_the_valid_manifest()
    portal.add_orphan("old-page", 3)

    exit_code = run_plan(products_root, portal, "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "ORPHAN toc-entry claims/old-page" in output


def test_prune_turns_the_orphan_into_a_delete_and_exits_two(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    portal = portal_matching_the_valid_manifest()
    portal.add_orphan("old-page", 3)

    exit_code = run_plan(products_root, portal, "acme", prune=True)

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "DELETE toc-entry claims/old-page" in output


def test_exceeding_max_deletes_aborts_before_any_operation_is_printed(
    products_root, capsys
):
    """§A.14 #9: the guardrail fires before anything else happens — at plan
    time that means before a single line of output."""
    write_product(products_root, "Claims", VALID_MANIFEST)
    portal = portal_matching_the_valid_manifest()
    for index in range(4):
        portal.add_orphan(f"old-{index}", 3 + index)

    with pytest.raises(ReconcileError, match="4 deletions"):
        run_plan(products_root, portal, "acme", prune=True, max_deletes=3)

    assert capsys.readouterr().out == ""


def test_prune_emptying_a_product_of_all_apis_warns_loudly(products_root, capsys):
    """§A.8: pruning a product's last API reference must warn. The manifest keeps
    only the page; the portal still carries the API reference, so with prune on it
    becomes a delete that leaves the product with no APIs for consumers."""
    page_only = copy.deepcopy(VALID_MANIFEST)
    page_only["contentMetadata"] = [VALID_MANIFEST["contentMetadata"][0]]  # the markdown page
    write_product(products_root, "Claims", page_only)
    portal = portal_matching_the_valid_manifest()  # page + the now-orphan API reference

    exit_code = run_plan(products_root, portal, "acme", prune=True)

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "DELETE toc-entry claims/claims-intake-api" in output
    assert "WARNING" in output and "no API references" in output


# --- the actual-state loader ------------------------------------------------


def test_loader_fills_page_bodies_before_the_diff(products_root):
    """The portal hands back a page entry with only a document id; the
    reconciler compares text, so the loader must fetch it (§A.6 step 2)."""
    actual = load_actual_state(portal_matching_the_valid_manifest(), "acme", {"claims"})

    pages = {entry.slug: entry for entry in actual[0].entries}
    assert pages["getting-started"].document.content == "# Hello"
    assert pages["getting-started"].document.id == "doc-1"


def test_loader_fetches_the_tree_only_for_products_the_repo_declares(products_root):
    """Product orphans need only the product list; their entries are never
    diffed, so the extra section/ToC/document calls are skipped."""
    portal = portal_matching_the_valid_manifest()
    portal.products.append(
        Product(name="Other", slug="other", description="", id="prod-2")
    )

    actual = load_actual_state(portal, "acme", {"claims"})

    assert portal.detailed_product_ids == ["prod-1"]
    by_slug = {product.slug: product for product in actual}
    assert by_slug["other"].entries == ()


# --- configuration ----------------------------------------------------------


def test_missing_environment_fails_cleanly(products_root, monkeypatch, capsys):
    """ADR-0001 rule 2: the environment is read in one place, and a missing
    variable is a named error, not a stack trace part-way through."""
    monkeypatch.delenv("SWAGGERHUB_API_KEY", raising=False)
    monkeypatch.delenv("PORTAL_SUBDOMAIN", raising=False)

    exit_code = main(["plan", "--products", str(products_root)])

    output = capsys.readouterr().out
    assert exit_code == EXIT_ERROR
    assert "FAILED" in output
    assert "SWAGGERHUB_API_KEY" in output
