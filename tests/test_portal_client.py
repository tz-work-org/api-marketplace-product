"""Tests for the read-only Portal API client.

The client makes HTTP calls, but these tests make none: a fake session stands in
for `requests.Session` and returns canned pages. That is the point — the author's
work laptop has no route to SmartBear, so the pagination walk and the portal
lookup must be provable offline (ADR-0001).

The things worth pinning down here are the parts with logic rather than a single
passthrough call: following pages to the end, confirming the subdomain match
rather than trusting the server filter, and mapping the wire format into models
(flattening the ToC tree, resolving parent slugs, splitting apiUrl from markdown).
"""

from __future__ import annotations

import pytest

from publisher.models import Document, Product, TocEntry
from publisher.portal_client import PortalClient, PortalError


class FakeResponse:
    """The slice of `requests.Response` the client actually uses."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Returns queued responses in order and records each call it was given."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, timeout: int | None = None):
        self.calls.append({"method": "GET", "url": url, "params": params or {}})
        return self._responses.pop(0)

    def post(self, url: str, json: dict | None = None, timeout: int | None = None):
        self.calls.append({"method": "POST", "url": url, "json": json or {}})
        return self._responses.pop(0)

    def patch(self, url: str, json: dict | None = None, timeout: int | None = None):
        self.calls.append({"method": "PATCH", "url": url, "json": json or {}})
        return self._responses.pop(0)

    def put(self, url: str, params: dict | None = None, timeout: int | None = None):
        self.calls.append({"method": "PUT", "url": url, "params": params or {}})
        return self._responses.pop(0)


def client_with(responses: list[FakeResponse]) -> PortalClient:
    """A PortalClient whose network session is replaced by a fake one."""
    client = PortalClient(api_key="unused")
    client.session = FakeSession(responses)
    return client


def page(items: list[dict], number: int, total_pages: int) -> dict:
    return {"page": {"number": number, "totalPages": total_pages}, "items": items}


def product_payload(slug: str) -> dict:
    return {"id": f"prod-{slug}", "name": slug.title(), "slug": slug}


def test_single_page_returns_all_items():
    client = client_with(
        [FakeResponse(page([product_payload("a"), product_payload("b")], 1, 1))]
    )

    products = client.list_products("portal-1")

    assert [product.slug for product in products] == ["a", "b"]
    assert all(isinstance(product, Product) for product in products)


def test_follows_pages_to_the_end():
    client = client_with(
        [
            FakeResponse(page([product_payload("a")], 1, 3)),
            FakeResponse(page([product_payload("b")], 2, 3)),
            FakeResponse(page([product_payload("c")], 3, 3)),
        ]
    )

    products = client.list_products("portal-1")

    assert [product.slug for product in products] == ["a", "b", "c"]


def test_missing_total_pages_is_treated_as_one_page():
    # A page block without totalPages must not loop forever.
    client = client_with(
        [FakeResponse({"page": {"number": 1}, "items": [product_payload("a")]})]
    )

    products = client.list_products("portal-1")

    assert [product.slug for product in products] == ["a"]


def test_products_map_to_models_without_owner_or_entries():
    client = client_with(
        [
            FakeResponse(
                page(
                    [
                        {
                            "id": "prod-1",
                            "name": "Claims",
                            "slug": "claims",
                            "description": "Claims APIs",
                            "public": True,
                            "hidden": False,
                        }
                    ],
                    1,
                    1,
                )
            )
        ]
    )

    product = client.list_products("portal-1")[0]

    assert product.id == "prod-1"
    assert product.slug == "claims"
    assert product.public is True
    assert product.owner is None  # portal has no ownership concept
    assert product.entries == ()  # entries come from a separate call


def test_accept_header_is_permissive_for_empty_write_responses():
    """A document patch returns no body; a strict application/json Accept makes
    the portal answer 406 (a live apply hit this). `*/*` accepts both the empty
    write responses and the JSON read responses (§A.2 §8.9)."""
    client = PortalClient(api_key="unused")

    assert client.session.headers["Accept"] == "*/*"


def test_list_calls_send_no_explicit_page_size():
    """Page-size maxima differ per endpoint (sections caps at 10, §A.2 §8.8), so
    an explicit size risks a 400 — a live apply hit exactly that. We send only
    the page number and let the server default the size."""
    client = client_with([FakeResponse(page([{"id": "sec-1"}], 1, 1))])

    client.get_default_section_id("prod-1")

    params = client.session.calls[0]["params"]
    assert "size" not in params
    assert params["page"] == 1


def test_get_default_section_id_takes_the_first():
    client = client_with(
        [FakeResponse(page([{"id": "sec-1"}, {"id": "sec-2"}], 1, 1))]
    )

    assert client.get_default_section_id("prod-1") == "sec-1"


def test_get_default_section_id_raises_when_none():
    client = client_with([FakeResponse(page([], 1, 1))])

    with pytest.raises(PortalError):
        client.get_default_section_id("prod-1")


def test_toc_tree_flattens_and_resolves_parent_slugs():
    tree = [
        {
            "id": "toc-guide",
            "slug": "guides",
            "title": "Guides",
            "order": 0,
            "content": {"type": "markdown", "documentId": "doc-guide"},
            "children": [
                {
                    "id": "toc-start",
                    "slug": "getting-started",
                    "title": "Getting Started",
                    "order": 0,
                    "content": {"type": "markdown", "documentId": "doc-start"},
                }
            ],
        },
        {
            "id": "toc-api",
            "slug": "claims-api",
            "title": "Claims API",
            "order": 1,
            "content": {"type": "apiUrl", "url": "https://api.example/claims/1.0.0"},
        },
    ]
    client = client_with([FakeResponse(page(tree, 1, 1))])

    entries = client.list_table_of_contents("sec-1")

    assert all(isinstance(entry, TocEntry) for entry in entries)
    by_slug = {entry.slug: entry for entry in entries}
    # The nested child resolves its parent by slug, not the opaque parentId.
    assert by_slug["getting-started"].parent_slug == "guides"
    assert by_slug["guides"].parent_slug is None
    # An API reference keeps its URL; a markdown page stashes the documentId
    # on a body-less Document and leaves content_url empty.
    assert by_slug["claims-api"].content_url == "https://api.example/claims/1.0.0"
    assert by_slug["claims-api"].document is None
    assert by_slug["getting-started"].content_url == ""
    assert by_slug["getting-started"].document == Document(content="", id="doc-start")


def test_get_document_maps_to_a_model():
    client = client_with(
        [FakeResponse({"id": "doc-1", "content": "# Hello"})]
    )

    document = client.get_document("doc-1")

    assert document == Document(content="# Hello", id="doc-1")


def test_get_portal_id_confirms_the_subdomain_match():
    client = client_with(
        [FakeResponse(page([{"id": "p-123", "subdomain": "acme-xmf"}], 1, 1))]
    )

    assert client.get_portal_id("acme-xmf") == "p-123"


def test_get_portal_id_raises_when_nothing_matches():
    client = client_with([FakeResponse(page([], 1, 1))])

    with pytest.raises(PortalError) as caught:
        client.get_portal_id("missing-xmf")

    assert caught.value.status_code == 404
    assert "missing-xmf" in str(caught.value)


def test_failed_response_raises_naming_the_resource():
    client = client_with([FakeResponse({"title": "Not Found"}, status_code=404)])

    with pytest.raises(PortalError) as caught:
        client.get_document("doc-9")

    assert "reading document doc-9" in str(caught.value)
    assert caught.value.status_code == 404


# --- writes: the create responses carry only ids, not full objects ----------
#
# The POST 201 bodies are `{id}` (product) and `{id, documentId}` (entry), not a
# whole resource (§A.2 §8.7). These pin that parsing — the gap a live apply
# found, because the read mappers were wrongly reused on create responses.


def test_create_product_parses_the_id_only_response():
    client = client_with([FakeResponse({"id": "prod-new"})])
    desired = Product(name="Claims", slug="claims", description="Claims APIs")

    created = client.create_product("portal-1", desired)

    assert created.id == "prod-new"
    assert created.slug == "claims"  # desired fields preserved
    body = client.session.calls[0]["json"]
    assert body["type"] == "new"
    assert body["name"] == "Claims"
    assert body["description"] == "Claims APIs"


def test_create_page_entry_parses_id_and_document_id():
    client = client_with([FakeResponse({"id": "toc-new", "documentId": "doc-new"})])
    entry = TocEntry(
        slug="getting-started",
        title="Getting Started",
        order=1,
        content_type="markdown",
        content_url="getting-started.md",
        document=Document(content="# Hi", source_path="getting-started.md"),
    )

    created = client.create_toc_entry("sec-1", entry, parent_id=None)

    assert created.id == "toc-new"
    assert created.document.id == "doc-new"  # server id, for the body write
    assert created.document.content == "# Hi"  # desired content preserved
    body = client.session.calls[0]["json"]
    assert body["type"] == "new"
    assert body["content"] == {"type": "markdown", "source": "external"}


def test_create_api_reference_entry_has_no_document_and_sends_its_url():
    client = client_with([FakeResponse({"id": "toc-api"})])  # no documentId
    entry = TocEntry(
        slug="claims-api",
        title="Claims API",
        order=2,
        content_type="apiUrl",
        content_url="https://api.example/claims/1.0.0/swagger.json",
    )

    created = client.create_toc_entry("sec-1", entry, parent_id="toc-parent")

    assert created.id == "toc-api"
    assert created.document is None
    body = client.session.calls[0]["json"]
    assert body["content"] == {
        "type": "apiUrl",
        "url": "https://api.example/claims/1.0.0/swagger.json",
    }
    assert body["parentId"] == "toc-parent"


def test_update_document_sends_content_in_the_body():
    client = client_with([FakeResponse({})])  # empty 200, nothing to parse

    client.update_document("doc-1", "# New words")

    call = client.session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/documents/doc-1")
    assert call["json"] == {"content": "# New words"}


def test_update_product_sends_only_the_changed_fields():
    client = client_with([FakeResponse({})])
    desired = Product(name="Claims", slug="claims", description="new words")

    client.update_product("prod-1", ("description",), desired)

    assert client.session.calls[0]["json"] == {"description": "new words"}


# --- publish (§A.15 step 8) -------------------------------------------------


def test_get_unpublished_changes_flattens_the_diff_and_keeps_the_kind():
    payload = {
        "added": [
            {
                "path": "docs/getting-started",
                "title": "Getting Started",
                "type": "markdown",
                "tableOfContentsId": "t1",
            }
        ],
        "modified": [{"path": "docs/intake", "title": "Intake", "type": "apiUrl"}],
        "removed": [],
        "missing": [],
        "reordered": True,
        "comments": [],
    }
    client = client_with([FakeResponse(payload)])

    changes = client.get_unpublished_changes("prod-1")

    assert changes.is_empty is False
    assert changes.reordered is True
    labelled = {(c.kind, c.content_type, c.path) for c in changes.changes}
    assert ("added", "markdown", "docs/getting-started") in labelled
    assert ("modified", "apiUrl", "docs/intake") in labelled


def test_get_unpublished_changes_with_nothing_staged_is_empty():
    payload = {"added": [], "modified": [], "removed": [], "missing": [], "reordered": False}
    client = client_with([FakeResponse(payload)])

    assert client.get_unpublished_changes("prod-1").is_empty


def test_publish_product_sends_the_preview_param_and_parses_messages():
    payload = {
        "validationMessages": [
            {
                "level": "error",
                "message": "URL not allowed",
                "errorCode": "URL_NOT_ALLOWED",
                "tableOfContentsId": "t1",
            },
            {"level": "warning", "message": "heads up"},
        ]
    }
    client = client_with([FakeResponse(payload)])

    messages = client.publish_product("prod-1", preview=True)

    call = client.session.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/products/prod-1/published-content")
    assert call["params"] == {"preview": True}
    assert [m.level for m in messages] == ["error", "warning"]
    assert messages[0].is_error and messages[0].error_code == "URL_NOT_ALLOWED"


def test_publish_product_with_no_messages_is_clean():
    client = client_with([FakeResponse({})])  # ValidationResponse may omit the list

    assert client.publish_product("prod-1", preview=False) == []
