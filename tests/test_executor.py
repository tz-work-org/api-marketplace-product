"""Tests for the executor (§A.15 step 7) — turning operations into API calls.

The executor is exercised with a fake client that records every call and hands
back model objects with server-assigned ids, so the whole thing is provable
offline (ADR-0001). What the real client puts on the wire is
test_portal_client.py's concern; here only the sequence and arguments of the
calls matter — did creates come parents-first, did a child get its parent's id,
did a page's body get written to the document the create returned.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest
from conftest import VALID_MANIFEST, write_product

from publisher.cli import EXIT_OK, run_apply
from publisher.executor import ExecutorError, apply
from publisher.models import Document, Operation, Product, TocEntry
from publisher.reconciler import reconcile


# --- fakes and builders -----------------------------------------------------


class FakePortalClient:
    """Records write calls and answers reads from canned data.

    Reads mirror test_plan's fake so `run_apply` can be driven end to end; the
    write methods return the same model shapes the real client does — a created
    product/entry carries the id the server would assign, and a created page
    carries the document id its body must be written to.
    """

    def __init__(self, products=(), entries=(), documents=None, *, omit_document_id=False):
        self.products = list(products)
        self.entries = list(entries)
        self.documents = documents or {}
        self.omit_document_id = omit_document_id
        self.calls: list[tuple] = []
        self._products = itertools.count(1)
        self._tocs = itertools.count(1)
        self._docs = itertools.count(1)

    # reads
    def get_portal_id(self, subdomain: str) -> str:
        return "portal-1"

    def list_products(self, portal_id: str) -> list[Product]:
        return list(self.products)

    def get_default_section_id(self, product_id: str) -> str:
        self.calls.append(("get_default_section_id", product_id))
        return f"sec-{product_id}"

    def list_table_of_contents(self, section_id: str) -> list[TocEntry]:
        return list(self.entries)

    def get_document(self, document_id: str) -> Document:
        return self.documents[document_id]

    # writes
    def create_product(self, portal_id: str, product: Product) -> Product:
        self.calls.append(("create_product", portal_id, product.slug))
        return replace(product, id=f"prod-{next(self._products)}")

    def update_product(self, product_id, changes, product) -> None:
        self.calls.append(("update_product", product_id, tuple(changes)))

    def create_toc_entry(self, section_id, entry, parent_id) -> TocEntry:
        self.calls.append(("create_toc_entry", section_id, entry.slug, parent_id))
        document = None
        if entry.document is not None and not self.omit_document_id:
            document = Document(content="", id=f"doc-{next(self._docs)}")
        return replace(entry, id=f"toc-{next(self._tocs)}", document=document)

    def update_toc_entry(self, toc_id, changes, entry) -> None:
        self.calls.append(("update_toc_entry", toc_id, tuple(changes)))

    def update_document(self, document_id, content) -> None:
        self.calls.append(("update_document", document_id, content))


def page(slug, title, order, body, parent=None) -> TocEntry:
    return TocEntry(
        slug=slug,
        title=title,
        order=order,
        content_type="markdown",
        content_url=f"{slug}.md",
        parent_slug=parent,
        document=Document(content=body, source_path=f"{slug}.md"),
    )


def api_ref(slug, title, order, url) -> TocEntry:
    return TocEntry(
        slug=slug, title=title, order=order, content_type="apiUrl", content_url=url
    )


def product(slug, entries=(), *, description="", **fields) -> Product:
    return Product(
        name=fields.get("name", slug.title()),
        slug=slug,
        description=description,
        entries=tuple(entries),
        public=fields.get("public", False),
        hidden=fields.get("hidden", False),
    )


def creates_of(client, method):
    return [call for call in client.calls if call[0] == method]


# --- creating a whole product ----------------------------------------------


def test_a_new_product_is_created_then_its_entries_parents_first():
    desired = product(
        "claims",
        [
            page("getting-started", "Getting Started", 1, "# Hello"),
            api_ref("intake", "Intake", 2, "https://api/x/swagger.json"),
            page("auth", "Auth", 1, "# Auth", parent="getting-started"),
        ],
    )
    operations = reconcile([desired], [])
    client = FakePortalClient()

    apply(operations, [], client, "portal-1", log=lambda op: None)

    # product before anything, section fetched once for it
    assert client.calls[0] == ("create_product", "portal-1", "claims")
    assert ("get_default_section_id", "prod-1") in client.calls

    order = [call[2] for call in creates_of(client, "create_toc_entry")]
    assert order.index("getting-started") < order.index("auth")  # parent first


def test_a_child_entry_is_created_with_its_parents_portal_id():
    desired = product(
        "claims",
        [
            page("getting-started", "Getting Started", 1, "# Hello"),
            page("auth", "Auth", 1, "# Auth", parent="getting-started"),
        ],
    )
    client = FakePortalClient()
    apply(reconcile([desired], []), [], client, "portal-1", log=lambda op: None)

    creates = creates_of(client, "create_toc_entry")
    parent_toc_id = next(c for c in creates if c[2] == "getting-started")  # returns toc-1
    child = next(c for c in creates if c[2] == "auth")
    assert child[3] == "toc-1"  # the id the parent's create returned


def test_a_page_writes_its_body_to_the_created_document_but_an_api_ref_does_not():
    desired = product(
        "claims",
        [
            page("getting-started", "Getting Started", 1, "# Hello"),
            api_ref("intake", "Intake", 2, "https://api/x/swagger.json"),
        ],
    )
    client = FakePortalClient()
    apply(reconcile([desired], []), [], client, "portal-1", log=lambda op: None)

    document_writes = creates_of(client, "update_document")
    assert document_writes == [("update_document", "doc-1", "# Hello")]  # only the page


def test_a_child_added_under_an_existing_parent_uses_the_existing_parents_id():
    """The parent is unchanged, so it has no create to learn its id from — it
    must come from the actual state the run was seeded with."""
    existing_parent = replace(
        page("getting-started", "Getting Started", 1, "# Hello"),
        id="toc-9",
        document=Document(content="# Hello", id="doc-9"),
    )
    desired = replace(
        product(
            "claims",
            [
                page("getting-started", "Getting Started", 1, "# Hello"),
                page("auth", "Auth", 1, "# Auth", parent="getting-started"),
            ],
        ),
        id="prod-9",
    )
    actual = replace(product("claims", [existing_parent]), id="prod-9")
    client = FakePortalClient()

    apply(reconcile([desired], [actual]), [actual], client, "portal-1", log=lambda op: None)

    child = next(c for c in creates_of(client, "create_toc_entry") if c[2] == "auth")
    assert child[3] == "toc-9"  # the existing parent's id, seeded from actual state


# --- updating an existing product ------------------------------------------


def test_a_changed_description_patches_only_that_field():
    desired = replace(product("claims", description="new words"), id="prod-9")
    actual = replace(product("claims", description="old words"), id="prod-9")

    apply(reconcile([desired], [actual]), [actual], FakePortalClient(), "portal-1", log=lambda op: None)


def test_update_product_sends_the_product_id_and_changed_fields():
    desired = replace(product("claims", description="new words"), id="prod-9")
    actual = replace(product("claims", description="old words"), id="prod-9")
    client = FakePortalClient()

    apply(reconcile([desired], [actual]), [actual], client, "portal-1", log=lambda op: None)

    assert ("update_product", "prod-9", ("description",)) in client.calls


def test_changed_page_text_patches_the_existing_document():
    desired_entry = page("getting-started", "Getting Started", 1, "# New")
    actual_entry = replace(
        page("getting-started", "Getting Started", 1, "# Old"),
        id="toc-9",
        document=Document(content="# Old", id="doc-9"),
    )
    desired = replace(product("claims", [desired_entry]), id="prod-9")
    actual = replace(product("claims", [actual_entry]), id="prod-9")
    client = FakePortalClient()

    apply(reconcile([desired], [actual]), [actual], client, "portal-1", log=lambda op: None)

    assert ("update_document", "doc-9", "# New") in client.calls


def test_a_renamed_entry_patches_its_title():
    desired_entry = api_ref("intake", "New Name", 1, "https://api/x")
    actual_entry = replace(api_ref("intake", "Old Name", 1, "https://api/x"), id="toc-9")
    desired = replace(product("claims", [desired_entry]), id="prod-9")
    actual = replace(product("claims", [actual_entry]), id="prod-9")
    client = FakePortalClient()

    apply(reconcile([desired], [actual]), [actual], client, "portal-1", log=lambda op: None)

    assert ("update_toc_entry", "toc-9", ("title",)) in client.calls


# --- refusals (§A.15: deletion is step 9; type flips out of scope) ----------


def test_changing_an_entrys_content_type_is_refused():
    desired_entry = api_ref("intake", "Intake", 1, "https://api/x")
    actual_entry = replace(
        page("intake", "Intake", 1, "# body"),
        id="toc-9",
        document=Document(content="# body", id="doc-9"),
    )
    desired = replace(product("claims", [desired_entry]), id="prod-9")
    actual = replace(product("claims", [actual_entry]), id="prod-9")

    with pytest.raises(ExecutorError, match="content type"):
        apply(reconcile([desired], [actual]), [actual], FakePortalClient(), "portal-1", log=lambda op: None)


def test_a_delete_operation_is_refused_not_performed():
    delete = Operation(verb="delete", resource="toc-entry", path="claims/old")
    client = FakePortalClient()

    with pytest.raises(ExecutorError, match="step 9"):
        apply([delete], [], client, "portal-1", log=lambda op: None)

    assert client.calls == []  # nothing was attempted


def test_a_page_create_with_no_returned_document_id_fails_loudly():
    desired = product("claims", [page("getting-started", "Getting Started", 1, "# Hello")])
    client = FakePortalClient(omit_document_id=True)

    with pytest.raises(ExecutorError, match="no document id"):
        apply(reconcile([desired], []), [], client, "portal-1", log=lambda op: None)


# --- run_apply end to end (cli wiring, §A.9 exit code) ----------------------


def test_run_apply_on_an_empty_portal_creates_everything_and_exits_zero(
    products_root, capsys
):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePortalClient(products=[])

    exit_code = run_apply(products_root, client, "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "CREATE product claims" in output
    assert "change(s) applied" in output
    assert any(call[0] == "create_product" for call in client.calls)
    # the markdown page's body ("# Hello", from write_product) is written
    assert any(call[0] == "update_document" and call[2] == "# Hello" for call in client.calls)
