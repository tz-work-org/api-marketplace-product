"""Perform the operations the reconciler produced, against the portal.

Steps 3→4 of §A.6: the reconciler decided *what* should change; this module makes
it happen, one Portal API call at a time, in the order the operations already
arrive — product first, then entries parents-first, then page bodies. It is the
mirror of the actual-state loader: the loader reads the portal into models, this
writes models back. Both are orchestration that `cli` owns and hands the pieces
to (§A.12).

Unlike the reconciler, this is not pure — it calls the client, which does I/O. It
still obeys ADR-0001: it never reads the environment, never exits the process, and
raises rather than prints. The caller logs each operation as it completes, so a
run that fails part-way still shows what succeeded — there is no rollback, and
re-running converges (§A.6).

**Scope (§A.15 steps 7 + 9): creates, updates, and — behind `--prune` — deletes.**
A delete is the soft-delete of a table-of-contents entry, and it reaches this
module only because the guardrails above it (`--prune`, `--max-deletes`, §A.8)
already decided it may. Product deletion is not performed here or anywhere in
MVP1: a product missing from the repository surfaces as an orphan, never a
delete (§A.8).

The one piece of state a run threads is *portal ids*. A newly created product or
entry has no id until the server issues one, and a child entry needs its parent's
id to be created. `_KnownIds` starts from the ids actual state already carries and
learns the rest as creates return — so operations can be executed in the order
they arrive without a second pass.
"""

from __future__ import annotations

from typing import Callable

from .models import Operation, Product
from .portal_client import PortalClient


class ExecutorError(RuntimeError):
    """An operation cannot be executed as asked.

    Raised for the shapes MVP1 does not perform — a product or document
    deletion (only table-of-contents entries can be pruned, §A.8), or a change
    (an entry's content type) that is out of scope. A genuine API failure is a
    `PortalError` from the client instead.
    """


def apply(
    operations: list[Operation],
    actual: list[Product],
    client: PortalClient,
    portal_id: str,
    log: Callable[[Operation], None],
) -> None:
    """Execute each operation in order, calling `log` once each has succeeded.

    `actual` seeds the portal ids that already exist (a product or entry read
    from the portal carries its id); creates add new ids as the server returns
    them. `log` receives each operation *after* it is done, so the caller prints
    a line per operation and progress stays visible even if a later one raises.
    """
    known = _KnownIds(actual, client, portal_id)

    for operation in operations:
        _perform(operation, client, known)
        log(operation)


class _KnownIds:
    """The portal ids a run needs, resolved as it goes.

    Seeded from actual state and extended by each create. Sections are fetched
    lazily and cached, because only a product that gains an entry needs one.
    """

    def __init__(
        self, actual: list[Product], client: PortalClient, portal_id: str
    ) -> None:
        self.client = client
        self.portal_id = portal_id
        self.product_id = {product.slug: product.id for product in actual}
        self.toc_id = {
            (product.slug, entry.slug): entry.id
            for product in actual
            for entry in product.entries
        }
        self._section_id: dict[str, str] = {}

    def section_for(self, product_slug: str) -> str:
        """The default section id of a product, fetched once and remembered."""
        if product_slug not in self._section_id:
            self._section_id[product_slug] = self.client.get_default_section_id(
                self.product_id[product_slug]
            )
        return self._section_id[product_slug]


def _perform(operation: Operation, client: PortalClient, known: _KnownIds) -> None:
    """Route one operation to the calls that carry it out."""
    if operation.resource == "product":
        _perform_product(operation, client, known)
    elif operation.resource == "toc-entry":
        _perform_toc_entry(operation, client, known)
    elif operation.resource == "document":
        _perform_document(operation, client, known)
    else:
        raise ExecutorError(
            f"cannot execute {operation.verb} {operation.resource} {operation.path}"
        )


def _perform_product(
    operation: Operation, client: PortalClient, known: _KnownIds
) -> None:
    if operation.verb == "create":
        created = client.create_product(known.portal_id, operation.desired)
        known.product_id[operation.path] = created.id  # path is the product slug
    elif operation.verb == "update":
        client.update_product(
            operation.actual.id, operation.changes, operation.desired
        )
    else:
        raise ExecutorError(
            f"{operation.verb} product is out of MVP1 scope "
            f"(retire via hidden, §A.8): {operation.path}"
        )


def _perform_toc_entry(
    operation: Operation, client: PortalClient, known: _KnownIds
) -> None:
    product_slug, entry_slug = operation.path.split("/", 1)

    if operation.verb == "create":
        entry = operation.desired
        parent_id = None
        if entry.parent_slug:
            parent_id = known.toc_id[(product_slug, entry.parent_slug)]

        created = client.create_toc_entry(
            known.section_for(product_slug), entry, parent_id
        )
        known.toc_id[(product_slug, entry_slug)] = created.id

        if entry.document is not None:
            _write_page_body(client, created, entry, operation.path)

    elif operation.verb == "update":
        if "content_type" in operation.changes:
            raise ExecutorError(
                f"changing an entry's content type is out of MVP1 scope: "
                f"{operation.path}"
            )
        client.update_toc_entry(
            operation.actual.id, operation.changes, operation.desired
        )

    elif operation.verb == "delete":
        # Soft-delete, children-first (the reconciler ordered them that way), so
        # `recursive` stays at its default False — see `delete_toc_entry`.
        client.delete_toc_entry(operation.actual.id)

    else:
        raise ExecutorError(
            f"{operation.verb} toc-entry is out of scope: {operation.path}"
        )


def _perform_document(
    operation: Operation, client: PortalClient, known: _KnownIds
) -> None:
    if operation.verb != "update":
        raise ExecutorError(
            f"{operation.verb} document is out of MVP1 scope: {operation.path}"
        )
    client.update_document(operation.actual.id, operation.desired.content)


def _write_page_body(client: PortalClient, created, entry, path: str) -> None:
    """Write a freshly created page's text into the document the server made.

    Creating the entry only makes an empty document; its id comes back on the
    create (§A.2 §8.3). If the portal returned no document id for a page, that is
    a contract violation worth failing loudly rather than dropping the body.
    """
    if created.document is None or not created.document.id:
        raise ExecutorError(
            f"portal returned no document id for page {path}; its body was not written"
        )
    client.update_document(created.document.id, entry.document.content)
