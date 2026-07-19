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
    """Returns queued responses in order and records the params it was given."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, timeout: int | None = None):
        self.calls.append({"url": url, "params": params or {}})
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
