"""Plain data structures describing desired and actual portal state.

This module knows about nothing. It imports no other publisher module, makes no
HTTP calls, and touches no files. Everything here is a frozen dataclass, so a
loaded manifest cannot be mutated half-way through a reconcile.

`Operation` is named in the module table (§A.12) but is not defined yet — it
belongs to the reconciler, which does not exist. Adding it now would be an
extension point for a hypothetical need, which §A.12 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass

# Content types a table-of-contents entry can carry. `markdown` and `html` point
# at a file in the repository; `apiUrl` points at a SwaggerHub-hosted API.
DOCUMENT_TYPES = ("markdown", "html")
API_REFERENCE_TYPE = "apiUrl"


@dataclass(frozen=True)
class Owner:
    """Who owns a product.

    Has no Portal API footprint — the portal has no concept of ownership. This
    exists to generate CODEOWNERS (§A.10), which is what gives ownership teeth
    on pull requests.
    """

    name: str
    email: str
    github_handle: str


@dataclass(frozen=True)
class Document:
    """The body text of a documentation page.

    Separate from the entry that points at it, because the portal models them as
    two resources: changing a page's text is a different call from changing its
    position in the navigation.

    `id` is the portal's `documentId`: `None` for a document loaded from the
    repository (desired state), populated for one read back from the portal
    (actual state). `source_path` is the reverse — set for desired state, empty
    for actual, which has no repo file behind it.
    """

    content: str
    source_path: str = ""  # Repo-relative, so error messages can name the real file.
    id: str | None = None


@dataclass(frozen=True)
class TocEntry:
    """One item in a product's navigation.

    Either an API reference or a documentation page. `slug` is identity and
    `title` is a mutable attribute — renaming a page updates it in place, while
    changing its slug declares a new entry and orphans the old one (§A.7).
    """

    slug: str
    title: str
    order: int
    content_type: str
    content_url: str
    parent_slug: str | None = None
    document: Document | None = None
    id: str | None = None  # Portal `tocId`: None for desired, set for actual state.

    @property
    def is_api_reference(self) -> bool:
        return self.content_type == API_REFERENCE_TYPE


@dataclass(frozen=True)
class Product:
    """A product as the repository declares it.

    `name` comes from the directory name rather than the manifest, because §A.4
    makes the folder name the product name. Keeping it out of the manifest means
    the two cannot disagree.

    One dataclass serves both desired and actual state (§A.12). The fields that
    only desired state has default to empty: a product read back from the portal
    carries an `id` but no `owner` (the portal has no ownership concept, §A.10)
    and its `entries` are fetched separately. A product loaded from a manifest is
    the mirror image — `owner` and `entries` set, `id` still `None`.
    """

    name: str
    slug: str
    description: str
    owner: Owner | None = None
    entries: tuple[TocEntry, ...] = ()
    public: bool = False
    hidden: bool = False
    auto_publish: bool = False
    logo: str | None = None
    logo_dark: str | None = None
    source_path: str = ""  # Repo-relative directory, for error messages.
    id: str | None = None  # Portal `productId`: None for desired, set for actual.

    @property
    def api_references(self) -> tuple[TocEntry, ...]:
        """Entries pointing at a SwaggerHub API.

        Used by the prune guardrail that warns when a change would leave a
        product with no APIs at all (§A.8).
        """
        return tuple(entry for entry in self.entries if entry.is_api_reference)
