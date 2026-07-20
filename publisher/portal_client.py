"""Thin HTTP client for the SwaggerHub Portal API.

One method per endpoint. No business logic, no decisions about what *should*
happen — that belongs in the executor, which hands this client an operation list
and calls it in dependency order (§A.12). Keeping the endpoint shapes in one
readable place means that when the Portal API changes there is exactly one file
to correct.

Every endpoint here was verified against the authoritative
`smartbear-public/swaggerhub-portal-api/0.8.0-beta` specification.
See `.local/verification/portal-api-verification.md` for the §A.2 gate.

**Read-only first (§A.15 step 4).** Only the state-fetch half exists so far:
enough to resolve the portal, its products, the default section, the
table-of-contents entries, and a document body. Create, update, delete and
publish arrive in later steps, once the reconciler can decide what to call.

**These methods return model objects, not raw response dictionaries (§A.12).**
The mapping from the Portal API's wire vocabulary — `title`, `parentId`, a
nested `content` block — into the same `Product`/`TocEntry`/`Document` models
that `manifest.py` produces lives here, in the one file that changes when the
API moves. It has moved once already (0.2.0-beta → 0.8.0-beta). The reconciler
then diffs desired against actual in a single vocabulary and never learns the
wire format.

Nothing here reads the environment — the caller passes an API key in, so the one
configuration function stays the only place `os.environ` is touched (ADR-0001,
rule 2). Nothing here exits the process either; failures raise `PortalError` and
the entry point decides what that means (ADR-0001, rule 1).
"""

from __future__ import annotations

import requests

from .models import API_REFERENCE_TYPE, Document, Product, TocEntry

# The real portal, not one of the two auto-mocking servers listed first in the
# specification. Those mock URLs return canned examples; this one is live state.
PORTAL_BASE_URL = "https://api.portal.swaggerhub.com/v1"

# The Portal API is a different service from the Registry API, with a different
# base URL and a different pagination shape (page/items here, offset/totalCount
# there). They deliberately share no code.
TIMEOUT_SECONDS = 30

# List endpoints page from 1 and wrap their results in {page, items}. Ask for a
# generous page size so the common small portal comes back in a single request;
# pagination is still followed in case it does not.
PAGE_SIZE = 100


class PortalError(RuntimeError):
    """A Portal API call failed.

    Carries the status code and body so the caller can print something that
    names the resource involved, rather than a bare stack trace. The Portal API
    returns RFC 7807 problem documents, so the body usually already reads well.
    """

    def __init__(self, message: str, status_code: int, body: str) -> None:
        super().__init__(f"{message} (HTTP {status_code}): {body}")
        self.status_code = status_code
        self.body = body


class PortalClient:
    """Calls the SwaggerHub Portal API on behalf of the publisher."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    # --- reads ------------------------------------------------------------

    def get_portal_id(self, subdomain: str) -> str:
        """Resolve a portal subdomain to its identifier.

        The subdomain is how a human names the target portal (§A.11); every
        other call needs the portal's UUID. This is the first call the publisher
        makes, so it doubles as the proof that authentication works.
        """
        portals = self._get_all_pages("/portals", {"subdomain": subdomain})

        # The filter is server-side, but confirm the match here rather than
        # trusting it — the same discipline the reconciler applies to slugs.
        for portal in portals:
            if portal.get("subdomain") == subdomain:
                return portal["id"]

        raise PortalError(
            f"no portal found with subdomain '{subdomain}'", 404, "empty result"
        )

    def list_products(self, portal_id: str) -> list[Product]:
        """Return every product in the portal, as `Product` models.

        Following all pages matters here more than anywhere: a product missed
        past the first page reads as absent to the reconciler, which would then
        propose deleting live content (§A.2 §4.5). Each product carries its
        portal `id` but no `owner` or `entries` — ownership has no portal
        footprint (§A.10) and entries come from a separate call.
        """
        payloads = self._get_all_pages(f"/portals/{portal_id}/products", {})
        return [_product_from_payload(payload) for payload in payloads]

    def get_default_section_id(self, product_id: str) -> str:
        """Return the id of the product's default section.

        Sections are read-only and not a diffed resource, so this returns just
        the id the ToC calls need rather than a section model. Section creation
        was removed from the API (§A.3); every product has one default section,
        and §A.6 resolves it by listing and taking the first.
        """
        sections = self._get_all_pages(f"/products/{product_id}/sections", {})
        if not sections:
            raise PortalError(
                f"product {product_id} has no sections", 404, "empty result"
            )
        return sections[0]["id"]

    def list_table_of_contents(self, section_id: str) -> list[TocEntry]:
        """Return the section's table-of-contents entries, as `TocEntry` models.

        The API returns a tree — top-level items each with a `children` array.
        This flattens it and resolves each child's `parentId` to its parent's
        slug, because our model matches entry slugs scoped to their parent
        (§A.7), not by the opaque id the wire format uses.
        """
        payloads = self._get_all_pages(f"/sections/{section_id}/table-of-contents", {})
        return _toc_entries_from_tree(payloads)

    def get_document(self, document_id: str) -> Document:
        """Return a document as a `Document` model, carrying its id and body.

        A documentation entry points at a document by id; its text is a separate
        resource, so reading a page's current content is a second call after the
        entry itself.
        """
        response = self.session.get(
            self._url(f"/documents/{document_id}"), timeout=TIMEOUT_SECONDS
        )
        self._check(response, f"reading document {document_id}")
        return _document_from_payload(response.json())

    # --- internals --------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{PORTAL_BASE_URL}{path}"

    def _check(self, response: requests.Response, what: str) -> None:
        """Raise a readable error unless the response succeeded.

        Every failure should name the thing that failed: a reconcile touches
        many products, and 'HTTP 404' on its own tells the reader nothing about
        which one.
        """
        if not response.ok:
            raise PortalError(what, response.status_code, response.text[:500])

    def _get_all_pages(self, path: str, params: dict) -> list[dict]:
        """Fetch every page of a paginated list endpoint and return all items.

        The Portal API wraps list results in `{page: {number, totalPages}, items}`
        and numbers pages from 1. This walks forward until the last page. If the
        server omits `totalPages` it is treated as a single page, which is the
        safe reading — better to under-report than to loop forever.
        """
        items: list[dict] = []
        page_number = 1

        while True:
            page_params = {**params, "page": page_number, "size": PAGE_SIZE}
            response = self.session.get(
                self._url(path), params=page_params, timeout=TIMEOUT_SECONDS
            )
            self._check(response, f"listing {path} (page {page_number})")

            payload = response.json()
            items.extend(payload.get("items", []))

            total_pages = payload.get("page", {}).get("totalPages", page_number)
            if page_number >= total_pages:
                return items
            page_number += 1


# --- payload mapping ------------------------------------------------------
#
# One function per resource, converting a Portal API payload into the model the
# rest of the publisher speaks. This is the only code that knows the wire names
# (`title`, `parentId`, the nested `content` block), so an API rename is a change
# here and nowhere else (§A.12).


def _product_from_payload(payload: dict) -> Product:
    """Map a product payload to a `Product` (actual state — no owner, no entries)."""
    return Product(
        name=payload["name"],
        slug=payload["slug"],
        description=payload.get("description") or "",
        public=payload.get("public", False),
        hidden=payload.get("hidden", False),
        id=payload["id"],
    )


def _document_from_payload(payload: dict) -> Document:
    """Map a document payload to a `Document`, carrying its id and body."""
    return Document(content=payload.get("content") or "", id=payload["id"])


def _toc_entries_from_tree(items: list[dict]) -> list[TocEntry]:
    """Flatten the ToC tree, resolving each entry's parent by slug.

    Top-level items are returned with a nested `children` array; the parent slug
    is carried down as the tree is walked, because entry identity is slug scoped
    to parent (§A.7). Order within the flat list is a top-down walk — the
    reconciler treats `order` as a relative sequence, not an absolute value.
    """
    entries: list[TocEntry] = []

    def walk(nodes: list[dict], parent_slug: str | None) -> None:
        for node in nodes:
            entries.append(_toc_entry_from_payload(node, parent_slug))
            walk(node.get("children") or [], node.get("slug"))

    walk(items, None)
    return entries


def _toc_entry_from_payload(payload: dict, parent_slug: str | None) -> TocEntry:
    """Map one ToC item to a `TocEntry`.

    An API reference carries its URL in the `content` block. A markdown or html
    page instead carries a `documentId` pointing at a separate document; that id
    is stashed on a body-less `Document` so the caller can fetch the text with
    `get_document`. The repo-relative `content_url` is a desired-state concept
    the portal has no equivalent for, so it is left empty for pages.
    """
    content = payload.get("content") or {}
    content_type = content.get("type", "")

    if content_type == API_REFERENCE_TYPE:
        content_url = content.get("url") or ""
        document = None
    else:
        content_url = ""
        document_id = content.get("documentId")
        document = Document(content="", id=document_id) if document_id else None

    return TocEntry(
        slug=payload["slug"],
        title=payload["title"],
        order=payload.get("order", 0),
        content_type=content_type,
        content_url=content_url,
        parent_slug=parent_slug,
        document=document,
        id=payload["id"],
    )
