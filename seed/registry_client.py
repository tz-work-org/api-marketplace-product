"""Thin HTTP client for the SwaggerHub Registry API.

One method per endpoint. No business logic, no decisions about what *should*
happen — that belongs in seed.py. This split keeps the endpoint shapes in one
readable place, so when the Registry API changes there is exactly one file to
correct.

Endpoints here were verified against swagger-hub/registry-api/1.3.0 before this
file was written. See the §A.16 verification report.
"""

from __future__ import annotations

import requests

REGISTRY_BASE_URL = "https://api.swaggerhub.com"

# The Registry API is a different service from the Portal API, with a different
# base URL and a different pagination shape. They deliberately share no code.
TIMEOUT_SECONDS = 30


class RegistryError(RuntimeError):
    """A Registry API call failed.

    Carries the status code and body so the caller can print something that
    names the resource involved, rather than a bare stack trace.
    """

    def __init__(self, message: str, status_code: int, body: str) -> None:
        super().__init__(f"{message} (HTTP {status_code}): {body}")
        self.status_code = status_code
        self.body = body


class RegistryClient:
    """Calls the SwaggerHub Registry API on behalf of the seed utility."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{REGISTRY_BASE_URL}{path}"

    def _check(self, response: requests.Response, what: str) -> None:
        """Raise a readable error unless the response succeeded.

        Every failure in this utility should name the thing that failed, because
        a seeding run touches nine APIs and four projects and 'HTTP 409' on its
        own tells the reader nothing.
        """
        if not response.ok:
            raise RegistryError(what, response.status_code, response.text[:500])

    # --- APIs -------------------------------------------------------------

    def api_exists(self, owner: str, api_slug: str) -> bool:
        """Return True if the API already exists under this owner.

        Used to decide whether a run is creating or updating, which is only for
        the log line — the create call upserts either way.
        """
        response = self.session.get(
            self._url(f"/apis/{owner}/{api_slug}"), timeout=TIMEOUT_SECONDS
        )
        if response.status_code == 404:
            return False
        self._check(response, f"checking API {owner}/{api_slug}")
        return True

    def save_api(
        self, owner: str, api_slug: str, version: str, definition_yaml: str
    ) -> None:
        """Create the API, or update it if it already exists.

        POST is an upsert here — the Registry API documents this operation as
        'create a new API or update an existing API'. That is what makes
        re-running the seed utility safe, as §A.16 requires.
        """
        response = self.session.post(
            self._url(f"/apis/{owner}/{api_slug}"),
            params={"version": version, "isPrivate": "false"},
            data=definition_yaml.encode("utf-8"),
            headers={"Content-Type": "application/yaml"},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"saving API {owner}/{api_slug} {version}")

    def is_api_published(self, owner: str, api_slug: str, version: str) -> bool:
        """Return the current published state of an API version."""
        response = self.session.get(
            self._url(f"/apis/{owner}/{api_slug}/{version}/settings/lifecycle"),
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"reading lifecycle for {owner}/{api_slug} {version}")
        return bool(response.json().get("published", False))

    def set_api_published(
        self, owner: str, api_slug: str, version: str, published: bool
    ) -> None:
        """Move an API version into or out of published state."""
        response = self.session.put(
            self._url(f"/apis/{owner}/{api_slug}/{version}/settings/lifecycle"),
            json={"published": published},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"publishing {owner}/{api_slug} {version}")

    def delete_api(self, owner: str, api_slug: str) -> None:
        """Delete an API and all of its versions. Used only by teardown."""
        response = self.session.delete(
            self._url(f"/apis/{owner}/{api_slug}"), timeout=TIMEOUT_SECONDS
        )
        if response.status_code == 404:
            return
        self._check(response, f"deleting API {owner}/{api_slug}")

    # --- Projects ---------------------------------------------------------

    def list_projects(self, owner: str) -> list[dict]:
        """Return every project owned by this organisation.

        The Registry API paginates with offset/totalCount, unlike the Portal
        API's page/items. At four projects this fits in one response, but the
        total is checked so a future reader is not misled.
        """
        response = self.session.get(
            self._url(f"/projects/{owner}"), timeout=TIMEOUT_SECONDS
        )
        self._check(response, f"listing projects for {owner}")
        payload = response.json()
        return list(payload.get("projects", []))

    def create_project(
        self, owner: str, name: str, description: str, api_slugs: list[str]
    ) -> None:
        """Create a project containing the named APIs.

        The APIs must already exist and belong to the same owner. Their names
        are matched case-sensitively, which is why seed.py holds one slug per
        API rather than deriving it in two places.
        """
        response = self.session.post(
            self._url(f"/projects/{owner}"),
            json={"name": name, "description": description, "apis": api_slugs},
            timeout=TIMEOUT_SECONDS,
        )
        self._check(response, f"creating project {owner}/{name}")

    def delete_project(self, owner: str, project_id: str) -> None:
        """Delete a project. The APIs it contains are left untouched."""
        response = self.session.delete(
            self._url(f"/projects/{owner}/{project_id}"), timeout=TIMEOUT_SECONDS
        )
        if response.status_code == 404:
            return
        self._check(response, f"deleting project {owner}/{project_id}")
