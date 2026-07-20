"""Tests for the reconciler — the pure diff (§A.6 step 3).

These are the §A.14 acceptance criteria that can be proven offline, written
before the reconciler itself. Everything operates on model objects, never raw
API dictionaries, and nothing imports `portal_client` (criterion 10) — which is
what keeps the suite runnable on a machine with no route to SmartBear.
"""

from __future__ import annotations

import dataclasses

import pytest

from publisher.models import Document, Product, TocEntry
from publisher.reconciler import ReconcileError, enforce_max_deletes, reconcile


# --- builders -------------------------------------------------------------
#
# Desired state as `manifest.py` would produce it, and an `as_actual` mirror
# that mimics what the portal reports after a successful apply: same
# attributes, portal ids everywhere, no owner (no portal footprint, §A.10),
# and page bodies filled in — the actual-state loader fetches document text
# before the diff runs.


def page(slug: str, order: int, *, title: str | None = None,
         parent: str | None = None, body: str = "# Hello") -> TocEntry:
    return TocEntry(
        slug=slug,
        title=title or slug.replace("-", " ").title(),
        order=order,
        content_type="markdown",
        content_url=f"{slug}.md",
        parent_slug=parent,
        document=Document(content=body, source_path=f"{slug}.md"),
    )


def api(slug: str, order: int, *, title: str | None = None,
        parent: str | None = None, url: str | None = None) -> TocEntry:
    return TocEntry(
        slug=slug,
        title=title or slug.replace("-", " ").title(),
        order=order,
        content_type="apiUrl",
        content_url=url or f"https://api.swaggerhub.com/apis/org/{slug}/1.0.0/swagger.json",
        parent_slug=parent,
    )


def product(slug: str = "claims", *, name: str = "Claims",
            description: str = "Claims APIs.", entries=(), public: bool = False,
            hidden: bool = False) -> Product:
    return Product(
        name=name,
        slug=slug,
        description=description,
        entries=tuple(entries),
        public=public,
        hidden=hidden,
    )


def as_actual(desired: Product) -> Product:
    """The portal's view of a product that matches its manifest exactly."""
    entries = tuple(
        dataclasses.replace(
            entry,
            # The portal has no repo-relative path for a page; only API
            # references carry a URL back.
            content_url=entry.content_url if entry.is_api_reference else "",
            document=(
                dataclasses.replace(entry.document, source_path="", id=f"doc-{entry.slug}")
                if entry.document
                else None
            ),
            id=f"toc-{entry.slug}",
        )
        for entry in desired.entries
    )
    return dataclasses.replace(
        desired, owner=None, source_path="", entries=entries, id=f"prod-{desired.slug}"
    )


def claims() -> Product:
    return product(entries=[page("getting-started", 1), api("claims-search", 2)])


# --- idempotency: the defining criteria (§A.14 #2, #3) ---------------------


def test_unchanged_state_plans_nothing():
    desired = claims()

    assert reconcile([desired], [as_actual(desired)]) == []


def test_first_run_creates_everything_second_run_is_empty():
    """Two consecutive applies; the second produces an empty plan (§A.14 #2).

    Offline analogue: reconcile against an empty portal plans only creates;
    reconcile against a portal mirroring the manifest plans nothing. This is
    what makes the program a reconciler, not an upsert script with ambitions.
    """
    desired = claims()

    first = reconcile([desired], [])
    assert [operation.verb for operation in first] == ["create"] * 3  # product + 2 entries

    assert reconcile([desired], [as_actual(desired)]) == []


# --- rename in place (§A.14 #4) --------------------------------------------


def test_renaming_an_entry_is_exactly_one_update():
    """Slug unchanged, title changed → one update, never a create + delete (§A.7)."""
    desired = product(entries=[page("getting-started", 1, title="Start Here")])
    actual = as_actual(product(entries=[page("getting-started", 1, title="Getting Started")]))

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].verb == "update"
    assert operations[0].resource == "toc-entry"
    assert operations[0].path == "claims/getting-started"
    assert operations[0].changes == ("title",)


def test_renaming_a_product_is_exactly_one_update():
    desired = product(name="Claims Handling")
    actual = as_actual(product(name="Claims"))

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].verb == "update"
    assert operations[0].resource == "product"
    assert operations[0].changes == ("name",)


def test_update_carries_the_actual_model_with_its_portal_id():
    """The executor needs the portal id to know what to PATCH (§A.12)."""
    desired = product(name="Claims Handling")
    actual = as_actual(product(name="Claims"))

    operations = reconcile([desired], [actual])

    assert operations[0].actual is actual
    assert operations[0].actual.id == "prod-claims"


# --- ordering is relative, not absolute (§A.14 #5, §A.7) -------------------


def test_middle_insert_is_one_create_and_no_renumbering():
    """Inserting an entry in the middle must not renumber everything after it."""
    actual = as_actual(product(entries=[page("a", 1), page("b", 2), page("c", 3)]))
    desired = product(entries=[page("a", 1), page("new", 2), page("b", 3), page("c", 4)])

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].verb == "create"
    assert operations[0].path == "claims/new"


def test_moving_one_entry_to_the_top_is_one_update():
    """The largest set already in relative order stays put; only the moved
    entry is touched — a plan reporting four changes for one drag is a plan
    nobody reads (§A.7)."""
    actual = as_actual(product(entries=[page("a", 1), page("b", 2), page("c", 3), page("d", 4)]))
    desired = product(entries=[page("d", 1), page("a", 2), page("b", 3), page("c", 4)])

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].verb == "update"
    assert operations[0].path == "claims/d"
    assert operations[0].changes == ("order",)


def test_swapping_two_adjacent_entries_updates_at_most_both():
    actual = as_actual(product(entries=[page("a", 1), page("b", 2)]))
    desired = product(entries=[page("b", 1), page("a", 2)])

    operations = reconcile([desired], [actual])

    assert all(operation.verb == "update" for operation in operations)
    assert all(operation.changes == ("order",) for operation in operations)
    assert len(operations) == 1  # moving either one past the other converges


def test_ordering_is_compared_within_each_parent_scope():
    """Reordering children under one parent must not move top-level entries."""
    actual = as_actual(product(entries=[
        page("guides", 1),
        page("x", 2, parent="guides"),
        page("y", 3, parent="guides"),
    ]))
    desired = product(entries=[
        page("guides", 1),
        page("y", 2, parent="guides"),
        page("x", 3, parent="guides"),
    ])

    operations = reconcile([desired], [actual])

    assert {operation.path for operation in operations} <= {"claims/x", "claims/y"}


# --- slug collisions are a hard error (§A.14 #6) ---------------------------


def test_two_products_on_one_slug_is_a_hard_error_naming_both():
    """The 22-character slug cap makes truncation collisions realistic; the
    answer is an error naming both products, never a silent suffix (§A.7)."""
    first = product(slug="policy-administratio", name="Policy Administration")
    second = product(slug="policy-administratio", name="Policy Administrators")

    with pytest.raises(ReconcileError) as caught:
        reconcile([first, second], [])

    assert "Policy Administration" in str(caught.value)
    assert "Policy Administrators" in str(caught.value)


def test_two_entries_on_one_slug_under_the_same_parent_is_a_hard_error():
    desired = product(entries=[
        page("overview", 1, title="First"),
        page("overview", 2, title="Second"),
    ])

    with pytest.raises(ReconcileError) as caught:
        reconcile([desired], [])

    assert "First" in str(caught.value)
    assert "Second" in str(caught.value)


def test_the_same_entry_slug_under_different_parents_is_fine():
    """Entry slugs are scoped to their parent, not global (§A.7)."""
    desired = product(entries=[
        page("guides", 1),
        page("reference", 2),
        page("overview", 3, parent="guides"),
        page("overview", 4, parent="reference"),
    ])

    operations = reconcile([desired], [])

    assert [operation.verb for operation in operations] == ["create"] * 5


# --- slug change = new resource + orphan (§A.14 #7) ------------------------


def test_changing_a_slug_is_a_create_plus_an_orphan():
    """Correct behaviour, but the plan must make it obvious so a reviewer
    catches an accidental slug edit before it destroys a consumer URL (§A.7)."""
    actual = as_actual(product(entries=[page("geting-started", 1)]))  # typo fixed below
    desired = product(entries=[page("getting-started", 1)])

    operations = reconcile([desired], [actual])

    verbs = {(operation.verb, operation.path) for operation in operations}
    assert ("create", "claims/getting-started") in verbs
    assert ("orphan", "claims/geting-started") in verbs
    assert len(operations) == 2


def test_moving_an_entry_under_a_different_parent_is_a_create_plus_an_orphan():
    """Identity is slug *scoped to parent* (§A.7), so reparenting declares a
    new entry and orphans the old one."""
    actual = as_actual(product(entries=[page("guides", 1), page("overview", 2)]))
    desired = product(entries=[page("guides", 1), page("overview", 2, parent="guides")])

    operations = reconcile([desired], [actual])

    verbs = [operation.verb for operation in operations]
    assert sorted(verbs) == ["create", "orphan"]


# --- prune guardrails (§A.14 #8, #9, §A.8) ---------------------------------


def test_orphans_with_prune_disabled_are_shown_but_not_deleted():
    actual = as_actual(claims())
    desired = product(entries=[page("getting-started", 1)])  # claims-search removed

    operations = reconcile([desired], [actual], prune=False)

    assert [operation.verb for operation in operations] == ["orphan"]
    assert operations[0].path == "claims/claims-search"


def test_prune_turns_entry_orphans_into_deletes():
    actual = as_actual(claims())
    desired = product(entries=[page("getting-started", 1)])

    operations = reconcile([desired], [actual], prune=True)

    assert [operation.verb for operation in operations] == ["delete"]
    assert operations[0].actual.id == "toc-claims-search"


def test_a_missing_product_is_always_an_orphan_never_a_delete():
    """Product deletion is out of MVP1 (§A.8): removing a folder prunes
    nothing, retirement is hidden: true."""
    operations = reconcile([], [as_actual(claims())], prune=True)

    assert [operation.verb for operation in operations] == ["orphan"]
    assert operations[0].resource == "product"


def test_deleted_children_come_before_their_parents():
    actual = as_actual(product(entries=[page("guides", 1), page("overview", 2, parent="guides")]))
    desired = product(entries=[])

    operations = reconcile([desired], [actual], prune=True)

    assert [operation.path for operation in operations] == [
        "claims/overview",
        "claims/guides",
    ]


def test_exceeding_max_deletes_aborts_before_anything_executes():
    operations = reconcile(
        [product(entries=[])],
        [as_actual(product(entries=[page("a", 1), page("b", 2), page("c", 3), page("d", 4)]))],
        prune=True,
    )

    with pytest.raises(ReconcileError) as caught:
        enforce_max_deletes(operations, max_deletes=3)

    assert "4" in str(caught.value)
    assert "claims/a" in str(caught.value)


def test_deletes_at_the_threshold_are_allowed():
    operations = reconcile(
        [product(entries=[])],
        [as_actual(product(entries=[page("a", 1), page("b", 2), page("c", 3)]))],
        prune=True,
    )

    enforce_max_deletes(operations, max_deletes=3)  # must not raise


def test_orphans_do_not_count_against_max_deletes():
    """With prune off nothing will be deleted, so nothing should trip the guard."""
    operations = reconcile(
        [product(entries=[])],
        [as_actual(product(entries=[page("a", 1), page("b", 2), page("c", 3), page("d", 4)]))],
        prune=False,
    )

    enforce_max_deletes(operations, max_deletes=3)  # must not raise


# --- document bodies -------------------------------------------------------


def test_changed_page_text_is_one_document_update():
    """A page is two portal resources (§A.3): text changes touch the document,
    not the entry."""
    actual = as_actual(product(entries=[page("getting-started", 1, body="# Old")]))
    desired = product(entries=[page("getting-started", 1, body="# New")])

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].verb == "update"
    assert operations[0].resource == "document"
    assert operations[0].path == "claims/getting-started"
    assert operations[0].actual.id == "doc-getting-started"  # what the executor PATCHes


def test_document_updates_come_after_structural_operations():
    """§A.6 execution order: entries before document bodies."""
    actual = as_actual(product(entries=[page("getting-started", 1, body="# Old")]))
    desired = product(entries=[
        page("getting-started", 1, body="# New"),
        api("claims-search", 2),
    ])

    operations = reconcile([desired], [actual])

    resources = [operation.resource for operation in operations]
    assert resources.index("toc-entry") < resources.index("document")


# --- new products ----------------------------------------------------------


def test_a_new_product_creates_parents_before_children():
    """§A.6: the portal cannot create a child before its parent has an id."""
    desired = product(entries=[
        page("overview", 1, parent="guides"),
        page("guides", 2),
    ])

    operations = reconcile([desired], [])

    paths = [operation.path for operation in operations]
    assert paths.index("claims/guides") < paths.index("claims/overview")
    assert paths[0] == "claims"  # the product itself comes first


def test_a_circular_parent_chain_is_a_hard_error():
    """The manifest loader rejects unknown parents but cannot see a cycle;
    fail fast here rather than looping forever."""
    desired = product(entries=[
        page("a", 1, parent="b"),
        page("b", 2, parent="a"),
    ])

    with pytest.raises(ReconcileError, match="circular"):
        reconcile([desired], [])


# --- attribute coverage ----------------------------------------------------


def test_visibility_changes_are_updates_with_named_fields():
    desired = product(public=True, hidden=True)
    actual = as_actual(product(public=False, hidden=False))

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].changes == ("public", "hidden")


def test_a_changed_api_reference_url_is_an_update():
    desired = product(entries=[api("claims-search", 1, url="https://api.swaggerhub.com/apis/org/claims-search/2.0.0/swagger.json")])
    actual = as_actual(product(entries=[api("claims-search", 1)]))

    operations = reconcile([desired], [actual])

    assert len(operations) == 1
    assert operations[0].changes == ("content_url",)


def test_owner_has_no_portal_footprint_and_is_never_diffed():
    """Owner exists for CODEOWNERS (§A.10); actual state never carries one."""
    from publisher.models import Owner

    desired = dataclasses.replace(
        claims(), owner=Owner(name="Jane Doe", email="jane@example.com", github_handle="jdoe")
    )

    assert reconcile([desired], [as_actual(claims())]) == []
