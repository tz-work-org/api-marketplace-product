"""Thin HTTP client for the SwaggerHub Portal API.

One method per endpoint. No business logic, no decisions about what *should*
happen — that belongs in the executor, which hands this client an operation list
and calls it in dependency order (§A.12). Keeping the endpoint shapes in one
readable place means that when the Portal API changes there is exactly one file
to correct.

Every endpoint here was verified against the authoritative
`smartbear-public/swaggerhub-portal-api/0.8.0-beta` specification.
See `.local/verification/portal-api-verification.md` for the §A.2 gate.

**Reads, creates, updates, publishes and deletes (§A.15 steps 4, 7, 8, 9).** The
state-fetch half resolves the portal, its products, the default section, the
table-of-contents entries, a document body and a product's unpublished changes;
the write half creates and patches products, entries and documents, and publishes
a product's draft to the live view. The one delete is a soft-delete of a
table-of-contents entry — the only destructive call, and it does nothing on its
own: it runs solely when `--prune` and the §A.8 guardrails above let it through.
Product deletion stays out of MVP1 (retire via `hidden`, §A.8).

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

from dataclasses import replace

import requests

from .models import (
    API_REFERENCE_TYPE,
    ContentChange,
    Document,
    Product,
    TocEntry,
    UnpublishedChanges,
    ValidationMessage,
)

# The real portal, not one of the two auto-mocking servers listed first in the
# specification. Those mock URLs return canned examples; this one is live state.
PORTAL_BASE_URL = "https://api.portal.swaggerhub.com/v1"

# The Portal API is a different service from the Registry API, with a different
# base URL and a different pagination shape (page/items here, offset/totalCount
# there). They deliberately share no code.
TIMEOUT_SECONDS = 30

# List endpoints page from 1 and wrap their results in {page, items}. We do not
# ask for a page size: the maximum differs per endpoint (10 for sections, 100 for
# portals, 500 for the table of contents, 1000 for products, §A.2 §8.8) and a
# size above an endpoint's cap is a 400. Each server default is generous enough
# that the common small portal returns in one page, and `_get_all_pages` follows
# every page regardless, so the default is both correct and safe everywhere.


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
        # Accept */* rather than application/json. Several write endpoints — a
        # document patch, for one — return an empty body, and a strict
        # application/json Accept makes the portal answer 406 "no acceptable
        # representation" for them. Read endpoints return JSON regardless, which
        # */* accepts, so one header serves both (§A.2 §8.9).
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "*/*"}
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

    def get_unpublished_changes(self, product_id: str) -> UnpublishedChanges:
        """Return the product's draft-vs-published diff, as the portal computes it.

        The platform tracks what has changed since the last publish itself
        (§A.6), so `publish` reads this rather than diffing again. Used to show
        what would go live and to skip a product with nothing staged.
        """
        response = self.session.get(
            self._url(f"/products/{product_id}/published-content/changes"),
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"reading unpublished changes for product {product_id}")
        return _unpublished_changes_from_payload(response.json())

    # --- writes -----------------------------------------------------------

    def create_product(self, portal_id: str, product: Product) -> Product:
        """Create a product and return it with the portal id the server assigned.

        `type: "new"` is the required write-only discriminator (§A.2 §8.2) that
        selects the create-from-scratch variant over the template `copy` one.

        The 201 body carries only the new `id` (§A.2 §8.7), not a full product —
        so the desired product is returned with that id filled in, which is all
        the executor needs to reach the product's section and create its entries.
        """
        body = {
            "type": "new",
            "name": product.name,
            "slug": product.slug,
            "public": product.public,
            "hidden": product.hidden,
        }
        if product.description:
            body["description"] = product.description

        response = self.session.post(
            self._url(f"/portals/{portal_id}/products"),
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"creating product {product.slug}")
        return replace(product, id=response.json()["id"])

    def update_product(
        self, product_id: str, changes: tuple[str, ...], product: Product
    ) -> None:
        """Patch only the product fields the reconciler found changed.

        `slug` is never here — it is identity, and a slug change is a create plus
        an orphan, not an update (§A.7). Sending just the changed fields keeps the
        request honest about what it means to alter.
        """
        body = _changed_fields(
            changes,
            {
                "name": product.name,
                "description": product.description,
                "public": product.public,
                "hidden": product.hidden,
            },
        )
        response = self.session.patch(
            self._url(f"/products/{product_id}"), json=body, timeout=TIMEOUT_SECONDS
        )
        self._check(response, f"updating product {product.slug}")

    def create_toc_entry(
        self, section_id: str, entry: TocEntry, parent_id: str | None
    ) -> TocEntry:
        """Create one table-of-contents entry and return it with its portal id.

        The 201 body carries only `{id, documentId}` (§A.2 §8.7), not a full
        entry — `documentId` is present for a page and absent for an API
        reference. The desired entry is returned with its `id` filled in and, for
        a page, its document carrying the server id so `update_document` can fill
        the body (§A.2 §8.3). `parent_id` is the portal id of the parent entry,
        resolved by the caller from the parent's own create; `None` for a
        top-level entry.
        """
        body = {
            "type": "new",
            "title": entry.title,
            "slug": entry.slug,
            "order": entry.order,
            "content": _content_reference(entry),
        }
        if parent_id:
            body["parentId"] = parent_id

        response = self.session.post(
            self._url(f"/sections/{section_id}/table-of-contents"),
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"creating toc-entry {entry.slug}")

        created = response.json()
        document = entry.document
        if document is not None:
            document = replace(document, id=created.get("documentId"))
        return replace(entry, id=created["id"], document=document)

    def update_toc_entry(
        self, toc_id: str, changes: tuple[str, ...], entry: TocEntry
    ) -> None:
        """Patch the table-of-contents entry fields that changed.

        `content_url` only appears for API references — a page's text lives on its
        document and is patched separately. `order` is a relative sequence the
        reconciler already resolved into a concrete value (§A.7). Note the change
        name `content_url` maps to the API's `content` block, not a field of the
        same name.
        """
        body: dict = {}
        if "title" in changes:
            body["title"] = entry.title
        if "order" in changes:
            body["order"] = entry.order
        if "content_url" in changes:
            body["content"] = _content_reference(entry)

        response = self.session.patch(
            self._url(f"/table-of-contents/{toc_id}"), json=body, timeout=TIMEOUT_SECONDS
        )
        self._check(response, f"updating toc-entry {entry.slug}")

    def update_document(self, document_id: str, content: str) -> None:
        """Write a page's body. `content` is the whole markdown, sent as a JSON
        string — the Portal API declares no size limit (§A.2 §8.6)."""
        response = self.session.patch(
            self._url(f"/documents/{document_id}"),
            json={"content": content},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"updating document {document_id}")

    def publish_product(
        self, product_id: str, preview: bool
    ) -> list[ValidationMessage]:
        """Promote a product's draft content to the live view.

        With `preview=True` the portal validates the draft and reports what it
        finds without publishing — the dry run that catches an unreachable API
        reference before consumers would see it. With `preview=False` it
        publishes. There is no request body; the platform already knows what has
        changed. Returns the validation messages either way (empty when clean).
        """
        response = self.session.put(
            self._url(f"/products/{product_id}/published-content"),
            params={"preview": preview},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"publishing product {product_id}")
        payload = response.json()
        return [
            _validation_message_from_payload(message)
            for message in payload.get("validationMessages", [])
        ]

    # --- deletes ----------------------------------------------------------

    def delete_toc_entry(self, toc_id: str, recursive: bool = False) -> None:
        """Soft-delete one table-of-contents entry (§A.8, step 9).

        The portal removes the entry from the *draft* up until the next publish,
        so live consumers are unaffected until `publish` runs — and it lists the
        entry at `GET /products/{id}/table-of-contents/removed`, from which a
        mistaken prune is restorable (`PATCH status="restored"`) before it goes
        live. `recursive` stays `False`: the executor deletes entries
        children-first, so each is already a leaf when its turn comes, and
        sweeping a whole subtree in one call would 404 on the children that
        follow. Success is `204` with no body (§A.2 step-9 gate).
        """
        response = self.session.delete(
            self._url(f"/table-of-contents/{toc_id}"),
            params={"recursive": recursive},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"deleting toc-entry {toc_id}")

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
        safe reading — better to under-report than to loop forever. No `size` is
        sent; the per-endpoint default applies (see the note by PORTAL_BASE_URL).
        """
        items: list[dict] = []
        page_number = 1

        while True:
            page_params = {**params, "page": page_number}
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


def _changed_fields(changes: tuple[str, ...], available: dict) -> dict:
    """Pick out the values for exactly the fields the reconciler flagged changed.

    Used where the reconciler's change names match the API's body keys (the
    product patch). A patch sends only what differs, so the request says what it
    means rather than re-stating the whole resource.
    """
    return {name: available[name] for name in changes if name in available}


def _content_reference(entry: TocEntry) -> dict:
    """Build the `content` block for a table-of-contents write.

    An API reference carries its `url`; a page (markdown or html) carries only
    its type and `source: "external"` — the repo owns the text, so in-portal
    editing is locked (§A.2 §8.4). The body itself is written separately with
    `update_document`; the create just brings the empty document into being.
    """
    if entry.is_api_reference:
        return {"type": API_REFERENCE_TYPE, "url": entry.content_url}
    return {"type": entry.content_type, "source": "external"}


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


def _unpublished_changes_from_payload(payload: dict) -> UnpublishedChanges:
    """Flatten the portal's draft-vs-published diff into one list of changes.

    The API groups changes by kind (`added`, `modified`, `removed`, `missing`),
    each a list of items carrying `path`, `title` and `type`. The kind is kept on
    each `ContentChange` so `publish` can label it. `comments` (unresolved review
    comments) are not part of the publish decision and are ignored.
    """
    changes = [
        ContentChange(
            kind=kind,
            path=item.get("path", ""),
            title=item.get("title", ""),
            content_type=item.get("type", ""),
        )
        for kind in ("added", "modified", "removed", "missing")
        for item in payload.get(kind) or []
    ]
    return UnpublishedChanges(
        changes=tuple(changes), reordered=payload.get("reordered", False)
    )


def _validation_message_from_payload(payload: dict) -> ValidationMessage:
    """Map one publish validation message to a `ValidationMessage`."""
    return ValidationMessage(
        level=payload.get("level", ""),
        message=payload.get("message", ""),
        error_code=payload.get("errorCode") or "",
        toc_id=payload.get("tableOfContentsId"),
    )


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
