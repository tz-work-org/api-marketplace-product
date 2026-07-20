"""Tests for `publish` (§A.15 step 8) — promoting draft content to the live view.

Driven with a fake client that records what it was asked to publish, so the whole
command is provable offline (ADR-0001). The portal computes its own draft-vs-live
diff, so the fake just hands back a canned `UnpublishedChanges`; here what matters
is which products get published, with which `preview` flag, and the exit code.
"""

from __future__ import annotations

from conftest import VALID_MANIFEST, write_product

from publisher.cli import (
    EXIT_CHANGES_PENDING,
    EXIT_ERROR,
    EXIT_OK,
    run_publish,
)
from publisher.models import (
    ContentChange,
    Product,
    UnpublishedChanges,
    ValidationMessage,
)


class FakePublishClient:
    """Reads products and their staged changes; records every publish call."""

    def __init__(self, products=(), changes=None, messages=None):
        self.products = list(products)
        self.changes = changes or {}  # product_id -> UnpublishedChanges
        self.messages = messages or {}  # product_id -> list[ValidationMessage]
        self.published: list[tuple[str, bool]] = []

    def get_portal_id(self, subdomain: str) -> str:
        return "portal-1"

    def list_products(self, portal_id: str) -> list[Product]:
        return list(self.products)

    def get_unpublished_changes(self, product_id: str) -> UnpublishedChanges:
        return self.changes.get(product_id, UnpublishedChanges())

    def publish_product(self, product_id: str, preview: bool):
        self.published.append((product_id, preview))
        return self.messages.get(product_id, [])


def portal_product(slug: str, product_id: str) -> Product:
    return Product(name=slug.title(), slug=slug, description="", id=product_id)


def staged(*kinds_and_slugs) -> UnpublishedChanges:
    return UnpublishedChanges(
        changes=tuple(
            ContentChange(kind=kind, path=f"docs/{slug}", title=slug.title(), content_type="markdown")
            for kind, slug in kinds_and_slugs
        )
    )


# --- preview (dry run) ------------------------------------------------------


def test_preview_shows_staged_changes_and_exits_two_without_publishing(
    products_root, capsys
):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": staged(("added", "getting-started"))},
    )

    exit_code = run_publish(products_root, client, "acme", preview=True)

    output = capsys.readouterr().out
    assert exit_code == EXIT_CHANGES_PENDING
    assert "PREVIEW product claims" in output
    assert "added markdown claims/getting-started" in output
    assert client.published == [("prod-1", True)]  # a preview call, not a live publish


def test_preview_with_nothing_staged_exits_zero(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": UnpublishedChanges()},
    )

    exit_code = run_publish(products_root, client, "acme", preview=True)

    assert exit_code == EXIT_OK


# --- publishing -------------------------------------------------------------


def test_publish_promotes_the_draft_and_exits_zero(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": staged(("added", "getting-started"))},
    )

    exit_code = run_publish(products_root, client, "acme", preview=False)

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "PUBLISH product claims" in output
    assert "1 product(s) published" in output
    assert client.published == [("prod-1", False)]


def test_a_product_with_nothing_staged_is_left_alone(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": UnpublishedChanges()},
    )

    exit_code = run_publish(products_root, client, "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "already published" in output
    assert client.published == []  # nothing was published


def test_a_product_not_in_the_portal_is_skipped(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(products=[])  # empty portal

    exit_code = run_publish(products_root, client, "acme")

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "not in portal" in output
    assert client.published == []


# --- validation failures ----------------------------------------------------


def test_a_validation_error_fails_the_publish(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": staged(("added", "settlement-api"))},
        messages={
            "prod-1": [
                ValidationMessage(
                    level="error",
                    message="URL not allowed",
                    error_code="URL_NOT_ALLOWED",
                    toc_id="t1",
                )
            ]
        },
    )

    exit_code = run_publish(products_root, client, "acme", preview=False)

    output = capsys.readouterr().out
    assert exit_code == EXIT_ERROR
    assert "ERROR [URL_NOT_ALLOWED]: URL not allowed" in output


def test_a_warning_alone_does_not_fail_the_publish(products_root, capsys):
    write_product(products_root, "Claims", VALID_MANIFEST)
    client = FakePublishClient(
        products=[portal_product("claims", "prod-1")],
        changes={"prod-1": staged(("added", "getting-started"))},
        messages={"prod-1": [ValidationMessage(level="warning", message="heads up")]},
    )

    exit_code = run_publish(products_root, client, "acme", preview=False)

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "WARNING: heads up" in output
