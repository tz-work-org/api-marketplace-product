"""Shared test fixtures and helpers.

No network anywhere in this suite. That is deliberate — §A.12 forbids I/O in the
core, which is what keeps these tests runnable on a machine with no route to
SmartBear (ADR-0001).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

VALID_MANIFEST = {
    "productMetadata": {
        "description": "APIs for claims intake, adjudication and settlement.",
        "slug": "claims",
        "owner": {
            "name": "Jane Doe",
            "email": "jane.doe@example.com",
            "githubHandle": "jdoe",
        },
    },
    "contentMetadata": [
        {
            "order": 1,
            "name": "Getting Started",
            "slug": "getting-started",
            "type": "markdown",
            "contentUrl": "getting-started.md",
        },
        {
            "order": 2,
            "name": "Claims Intake API",
            "slug": "claims-intake-api",
            "type": "apiUrl",
            "contentUrl": "https://api.swaggerhub.com/apis/org/claims-intake-api/1.0.0/swagger.json",
        },
    ],
}


@pytest.fixture
def products_root(tmp_path: Path) -> Path:
    """An empty products/ directory in a temporary location."""
    root = tmp_path / "products"
    root.mkdir()
    return root


def manifest_with(**product_metadata) -> dict:
    """A valid manifest with productMetadata fields overridden.

    Deep-copied so a test that mutates its manifest cannot affect the next one.
    """
    manifest = copy.deepcopy(VALID_MANIFEST)
    manifest["productMetadata"].update(product_metadata)
    return manifest


def write_product(
    root: Path, directory_name: str, manifest: dict, *, with_markdown: bool = True
) -> Path:
    """Write one product directory into a products root."""
    product_directory = root / directory_name
    product_directory.mkdir(parents=True)
    (product_directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_markdown:
        (product_directory / "getting-started.md").write_text("# Hello", encoding="utf-8")
    return product_directory
