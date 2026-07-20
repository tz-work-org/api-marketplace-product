"""The diff: compare desired state against actual state, emit operations.

Step 3 of the reconciliation algorithm (§A.6). Everything here is a pure
function of its arguments — no I/O, no HTTP, no filesystem, and no import of
`portal_client` (§A.12, the one architectural rule). That is what keeps the
diff unit-testable on a machine with no route to SmartBear.

The caller assembles both sides before calling in. `desired` comes from
`manifest.load_all_products`; `actual` comes from the portal with entries
attached and page bodies filled in — that loader arrives with `plan` (§A.15
step 6). This module never fetches anything.

Identity is slug (§A.7): products match by slug, entries by slug scoped to
their parent. A rename updates in place; a slug change is a create plus an
orphan. Order is compared as relative sequence, never as absolute numbers.
"""

from __future__ import annotations

from .models import Operation, Product, TocEntry


class ReconcileError(ValueError):
    """Desired state is internally inconsistent, or a guardrail tripped.

    Raised before any operation could execute. An identity collision, for
    example, would otherwise let list order decide which product wins.
    """


def reconcile(
    desired: list[Product], actual: list[Product], prune: bool = False
) -> list[Operation]:
    """Diff desired against actual and return the operations that converge them.

    With `prune` off (the default, §A.8) deletion candidates come back as
    `orphan` operations: the plan shows them, nothing acts on them. With
    `prune` on, table-of-contents orphans become `delete` operations. A whole
    product missing from the repository is always an orphan — product deletion
    is out of MVP1, retirement is `hidden: true`.
    """
    _reject_colliding_product_slugs(desired)

    operations: list[Operation] = []
    actual_by_slug = {product.slug: product for product in actual}

    for product in desired:
        _reject_colliding_entry_slugs(product)
        match = actual_by_slug.get(product.slug)
        if match is None:
            operations.extend(_create_whole_product(product))
        else:
            operations.extend(_converge_product(product, match, prune))

    desired_slugs = {product.slug for product in desired}
    for product in actual:
        if product.slug not in desired_slugs:
            operations.append(
                Operation(verb="orphan", resource="product", path=product.slug, actual=product)
            )

    return operations


def enforce_max_deletes(operations: list[Operation], max_deletes: int) -> None:
    """Abort when a plan wants more deletions than the threshold allows (§A.8).

    Called before anything executes, so exceeding the limit fails the whole
    run rather than truncating it. Orphans do not count — nothing will act on
    them.
    """
    deletions = [operation for operation in operations if operation.verb == "delete"]
    if len(deletions) > max_deletes:
        listed = ", ".join(operation.path for operation in deletions)
        raise ReconcileError(
            f"Plan wants {len(deletions)} deletions but --max-deletes is "
            f"{max_deletes}: {listed}. Nothing was executed."
        )


def products_emptied_by_prune(
    desired: list[Product], operations: list[Operation]
) -> list[str]:
    """Slugs of products a prune would strip of their last API reference (§A.8).

    Deleting an API reference is what leaves a product empty to consumers, so
    the check looks only at `delete` operations that remove an API-reference
    entry, and only fires when the product's desired state keeps none. Pure —
    it returns the slugs; the caller decides how loudly to warn (ADR-0001 #3).
    """
    losing_api_refs = {
        operation.path.split("/", 1)[0]
        for operation in operations
        if operation.verb == "delete"
        and operation.resource == "toc-entry"
        and operation.actual is not None
        and operation.actual.is_api_reference
    }
    return [
        product.slug
        for product in desired
        if product.slug in losing_api_refs and not product.api_references
    ]


# --- one product ----------------------------------------------------------


def _create_whole_product(product: Product) -> list[Operation]:
    """Plan a product the portal does not have at all: everything is a create.

    Entries come parents-first (§A.6) — the executor cannot create a child
    until the portal has issued its parent's id. A page's body travels inside
    its entry's create; writing it is part of bringing the entry into being,
    not a separate decision.
    """
    operations = [
        Operation(verb="create", resource="product", path=product.slug, desired=product)
    ]
    for entry in _parents_first(product.entries, product.entries, product.slug):
        operations.append(
            Operation(
                verb="create",
                resource="toc-entry",
                path=f"{product.slug}/{entry.slug}",
                desired=entry,
            )
        )
    return operations


def _converge_product(product: Product, actual: Product, prune: bool) -> list[Operation]:
    """Plan the changes for a product that exists on both sides.

    Output follows the execution order of §A.6: the product itself, then
    entries (parents before children), then page bodies, then deletions
    (children before parents).
    """
    operations: list[Operation] = []

    changes = _changed_product_fields(product, actual)
    if changes:
        operations.append(
            Operation(
                verb="update",
                resource="product",
                path=product.slug,
                desired=product,
                actual=actual,
                changes=changes,
            )
        )

    actual_by_key = {(entry.parent_slug, entry.slug): entry for entry in actual.entries}
    moved = _entries_needing_a_move(product.entries, actual.entries)
    document_updates: list[Operation] = []

    for entry in _parents_first(product.entries, product.entries, product.slug):
        key = (entry.parent_slug, entry.slug)
        path = f"{product.slug}/{entry.slug}"
        match = actual_by_key.get(key)

        if match is None:
            operations.append(
                Operation(verb="create", resource="toc-entry", path=path, desired=entry)
            )
            continue

        changes = _changed_entry_fields(entry, match)
        if key in moved:
            changes = changes + ("order",)
        if changes:
            operations.append(
                Operation(
                    verb="update",
                    resource="toc-entry",
                    path=path,
                    desired=entry,
                    actual=match,
                    changes=changes,
                )
            )

        if _page_text_differs(entry, match):
            document_updates.append(
                Operation(
                    verb="update",
                    resource="document",
                    path=path,
                    desired=entry.document,
                    actual=match.document,
                    changes=("content",),
                )
            )

    operations.extend(document_updates)
    operations.extend(_entry_removals(product, actual, prune))
    return operations


def _entry_removals(product: Product, actual: Product, prune: bool) -> list[Operation]:
    """Entries the portal has and the repository no longer wants.

    Children come before parents, the reverse of creation order (§A.6). With
    prune off they are orphans: displayed, never executed (§A.8).
    """
    desired_keys = {(entry.parent_slug, entry.slug) for entry in product.entries}
    removed = [
        entry for entry in actual.entries
        if (entry.parent_slug, entry.slug) not in desired_keys
    ]

    verb = "delete" if prune else "orphan"
    return [
        Operation(
            verb=verb,
            resource="toc-entry",
            path=f"{product.slug}/{entry.slug}",
            actual=entry,
        )
        for entry in reversed(_parents_first(removed, actual.entries, product.slug))
    ]


# --- field comparison ------------------------------------------------------


def _changed_product_fields(desired: Product, actual: Product) -> tuple[str, ...]:
    """Product attributes that differ, by name.

    Owner, logo and autoPublish are deliberately absent: owner has no portal
    footprint (§A.10), logo upload is deferred, and autoPublish drives
    publisher behaviour rather than portal state.
    """
    changes = []
    if desired.name != actual.name:
        changes.append("name")
    if desired.description != actual.description:
        changes.append("description")
    if desired.public != actual.public:
        changes.append("public")
    if desired.hidden != actual.hidden:
        changes.append("hidden")
    return tuple(changes)


def _changed_entry_fields(desired: TocEntry, actual: TocEntry) -> tuple[str, ...]:
    """Entry attributes that differ, by name — everything mutable except
    `order`, which is judged by relative sequence in `_entries_needing_a_move`.

    `content_url` only counts for API references: for a markdown or html page
    the desired value is a repo-relative path the portal has no equivalent for
    (the text lives on the document, compared separately).
    """
    changes = []
    if desired.title != actual.title:
        changes.append("title")
    if desired.content_type != actual.content_type:
        changes.append("content_type")
    if desired.is_api_reference and desired.content_url != actual.content_url:
        changes.append("content_url")
    return tuple(changes)


def _page_text_differs(desired: TocEntry, actual: TocEntry) -> bool:
    """True when a page's text needs writing.

    The actual-state loader fills `actual.document.content` before the diff
    runs. A matched entry whose actual side has no document at all — say it
    was an API reference until this change — still needs its text written.
    """
    if desired.document is None:
        return False
    if actual.document is None:
        return True
    return desired.document.content != actual.document.content


# --- ordering: relative sequence, not absolute numbers (§A.7) --------------


def _entries_needing_a_move(
    desired: tuple[TocEntry, ...], actual: tuple[TocEntry, ...]
) -> set[tuple[str | None, str]]:
    """The (parent, slug) keys of entries whose position must change.

    Compared scope by scope: within each parent, only entries present on both
    sides are looked at, so inserting a new entry in the middle moves nothing.
    Among the shared entries, the largest set already in the right relative
    order stays put and everything else moves — dragging one entry to the top
    is one move, not a renumbering of the whole list.
    """
    moved: set[tuple[str | None, str]] = set()

    for parent_slug in {entry.parent_slug for entry in desired}:
        desired_sequence = _sequence_under(parent_slug, desired)
        actual_sequence = _sequence_under(parent_slug, actual)

        shared = set(desired_sequence) & set(actual_sequence)
        desired_shared = [slug for slug in desired_sequence if slug in shared]
        actual_shared = [slug for slug in actual_sequence if slug in shared]

        for slug in _out_of_sequence(desired_shared, actual_shared):
            moved.add((parent_slug, slug))

    return moved


def _sequence_under(
    parent_slug: str | None, entries: tuple[TocEntry, ...]
) -> list[str]:
    """The slugs under one parent, in `order` order."""
    siblings = [entry for entry in entries if entry.parent_slug == parent_slug]
    return [entry.slug for entry in sorted(siblings, key=lambda entry: entry.order)]


def _out_of_sequence(desired_order: list[str], actual_order: list[str]) -> list[str]:
    """The smallest set of slugs that must move for actual to match desired.

    Both lists hold the same slugs. Map the actual order onto desired
    positions; whatever forms the longest already-ascending run can stay, and
    everything outside it moves.
    """
    position = {slug: index for index, slug in enumerate(desired_order)}
    positions_in_actual_order = [position[slug] for slug in actual_order]
    staying = _longest_ascending_run(positions_in_actual_order)
    return [slug for index, slug in enumerate(actual_order) if index not in staying]


def _longest_ascending_run(values: list[int]) -> set[int]:
    """Indexes of one longest strictly-ascending subsequence of `values`.

    Textbook dynamic programming, in the O(n²) form because a table of
    contents is dozens of entries at most and the obvious version is the
    readable one (§A.12): the best run ending at each element is one longer
    than the best run ending at any earlier, smaller element.
    """
    if not values:
        return set()

    best_length = [1] * len(values)
    came_from: list[int | None] = [None] * len(values)

    for here in range(len(values)):
        for earlier in range(here):
            longer = best_length[earlier] + 1
            if values[earlier] < values[here] and longer > best_length[here]:
                best_length[here] = longer
                came_from[here] = earlier

    staying: set[int] = set()
    index: int | None = best_length.index(max(best_length))
    while index is not None:
        staying.add(index)
        index = came_from[index]
    return staying


# --- tree order and identity checks ----------------------------------------


def _parents_first(
    entries: list[TocEntry] | tuple[TocEntry, ...],
    tree: tuple[TocEntry, ...],
    product_slug: str,
) -> list[TocEntry]:
    """Sort `entries` so every parent comes before its children.

    Depth in the tree is enough: top-level entries first, then their children,
    then grandchildren, keeping the given order within each depth. `tree` is
    the full entry list the parents live in — it can be wider than `entries`,
    e.g. when sorting only the removed entries of a product.
    """
    by_slug = {entry.slug: entry for entry in tree}

    def depth(entry: TocEntry) -> int:
        steps = 0
        while entry.parent_slug is not None:
            entry = by_slug[entry.parent_slug]
            steps += 1
            if steps > len(tree):
                raise ReconcileError(
                    f"Product '{product_slug}': circular parent chain involving "
                    f"entry '{entry.slug}'."
                )
        return steps

    return sorted(entries, key=depth)


def _reject_colliding_product_slugs(desired: list[Product]) -> None:
    """Two products on one slug is an identity collision (§A.7).

    The 22-character slug cap makes this realistic — name-derived slugs
    truncate. The answer is a hard error naming both products, never a silent
    suffix.
    """
    first_claim: dict[str, Product] = {}
    for product in desired:
        earlier = first_claim.get(product.slug)
        if earlier is not None:
            raise ReconcileError(
                f"Slug collision: products '{earlier.name}' and '{product.name}' "
                f"both resolve to slug '{product.slug}'. Slug is identity — "
                f"rename one."
            )
        first_claim[product.slug] = product


def _reject_colliding_entry_slugs(product: Product) -> None:
    """Two entries on one slug under the same parent is the same collision one
    level down — entry identity is slug scoped to parent (§A.7)."""
    first_claim: dict[tuple[str | None, str], TocEntry] = {}
    for entry in product.entries:
        key = (entry.parent_slug, entry.slug)
        earlier = first_claim.get(key)
        if earlier is not None:
            scope = f"under '{entry.parent_slug}'" if entry.parent_slug else "at top level"
            raise ReconcileError(
                f"Slug collision in product '{product.name}': entries "
                f"'{earlier.title}' and '{entry.title}' both use slug "
                f"'{entry.slug}' {scope}."
            )
        first_claim[key] = entry
