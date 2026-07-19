"""Seed the trial SwaggerHub organisation with realistic APIs and projects.

This is a test fixture, not part of the product publisher (§A.16). No publisher
module imports it, and the publisher must remain buildable and testable with
this directory absent.

Why it exists: the publisher cannot be exercised without products to reconcile,
and products cannot exist without published APIs to reference.

Usage:
    python3 seed/seed.py --dry-run     # print what would happen, call nothing
    python3 seed/seed.py               # create and publish
    python3 seed/seed.py --teardown --confirm
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from registry_client import RegistryClient, RegistryError

SPECS_DIRECTORY = Path(__file__).parent / "specs"


@dataclass(frozen=True)
class SeedApi:
    """One API to create in Swagger Studio.

    The slug is held explicitly rather than derived from the specification file,
    because the Registry API matches API names case-sensitively when they are
    listed in a project. Deriving it in two places is how that goes wrong.
    """

    slug: str
    version: str
    spec_filename: str
    publish: bool = True


@dataclass(frozen=True)
class SeedProject:
    """One project grouping several APIs."""

    name: str
    description: str
    api_slugs: list[str] = field(default_factory=list)


# The estate to create. Four projects, nine APIs, insurance-flavoured.
#
# Party Contact is deliberately left unpublished. A manifest that references it
# should fail clearly rather than confusingly, and there is no way to test that
# without an API that cannot be linked.
SEED_APIS: list[SeedApi] = [
    SeedApi("claims-intake-api", "1.0.0", "claims-intake.yaml"),
    SeedApi("claims-search-api", "1.0.0", "claims-search.yaml"),
    SeedApi("claims-status-api", "1.0.0", "claims-status.yaml"),
    SeedApi("settlement-api", "1.0.0", "settlement.yaml"),
    SeedApi("recovery-api", "1.0.0", "recovery.yaml"),
    SeedApi("policy-issuance-api", "1.0.0", "policy-issuance.yaml"),
    SeedApi("policy-endorsement-api", "1.0.0", "policy-endorsement.yaml"),
    SeedApi("party-search-api", "1.0.0", "party-search.yaml"),
    SeedApi("party-contact-api", "1.0.0", "party-contact.yaml", publish=False),
]

# Claims deliberately spans claims-core and claims-payments, so that a portal
# product drawing APIs from more than one project can be tested (§A.5, §B.10).
SEED_PROJECTS: list[SeedProject] = [
    SeedProject(
        "claims-core",
        "First notification of loss, search and claim lifecycle.",
        ["claims-intake-api", "claims-search-api", "claims-status-api"],
    ),
    SeedProject(
        "claims-payments",
        "Settlement of approved claims and recovery from third parties.",
        ["settlement-api", "recovery-api"],
    ),
    SeedProject(
        "policy-admin",
        "Policy issuance and mid-term adjustment.",
        ["policy-issuance-api", "policy-endorsement-api"],
    ),
    SeedProject(
        "party-services",
        "Party identity resolution and contact consent.",
        ["party-search-api", "party-contact-api"],
    ),
]


def log(action: str, kind: str, name: str, detail: str = "") -> None:
    """Print one line per operation, in the publisher's logging style.

    Format matches §A.12's example — `CREATE toc-entry claims/getting-started`
    — so a reader moving between the two programs sees the same shape.
    """
    line = f"{action:<8} {kind:<8} {name}"
    if detail:
        line = f"{line}  ({detail})"
    print(line)


def read_specification(spec_filename: str) -> str:
    """Read a specification file as text.

    Returned as raw text rather than parsed: the Registry API accepts YAML
    directly, so parsing would add a dependency (PyYAML) that §A.12's
    dependency budget does not allow.
    """
    path = SPECS_DIRECTORY / spec_filename
    if not path.exists():
        raise FileNotFoundError(f"Specification not found: {path}")
    return path.read_text(encoding="utf-8")


def read_configuration() -> tuple[str, str]:
    """Read the API key and organisation from the environment.

    Environment variables only, matching the publisher (§A.11), so the two
    programs are configured the same way.
    """
    api_key = os.environ.get("SWAGGERHUB_API_KEY", "").strip()
    organisation = os.environ.get("SWAGGERHUB_ORG", "").strip()

    missing = []
    if not api_key:
        missing.append("SWAGGERHUB_API_KEY")
    if not organisation:
        missing.append("SWAGGERHUB_ORG")
    if missing:
        raise SystemExit(f"Missing environment variable(s): {', '.join(missing)}")

    return api_key, organisation


def seed_one_api(
    client: RegistryClient, organisation: str, api: SeedApi, dry_run: bool
) -> None:
    """Create or update one API, then set its published state.

    A published API cannot be overwritten — the Registry API rejects the save
    with `409 Published APIs can not be overwritten`, and the `force` parameter
    does not override it. So an existing published API is unpublished, written,
    and published again. That three-step dance is the only way a second run
    succeeds, and §A.16 requires re-running to be safe.

    Publishing is applied every run rather than only on creation, so that an API
    someone unpublished by hand is put back the way the fixture declares it.
    """
    target = f"{organisation}/{api.slug}"

    if dry_run:
        log("WOULD", "api", target, f"save {api.version} from {api.spec_filename}")
        log("WOULD", "api", target, "publish" if api.publish else "leave unpublished")
        return

    exists = client.api_exists(organisation, api.slug)

    if exists and client.is_api_published(organisation, api.slug, api.version):
        client.set_api_published(organisation, api.slug, api.version, False)
        log("UNPUBLISH", "api", target, "temporarily — published APIs cannot be overwritten")

    client.save_api(organisation, api.slug, api.version, read_specification(api.spec_filename))
    log("UPDATE" if exists else "CREATE", "api", target, api.version)

    if not api.publish:
        log("SKIP", "api", target, "left unpublished by design")
        return

    client.set_api_published(organisation, api.slug, api.version, True)
    log("PUBLISH", "api", target, api.version)


def seed_one_project(
    client: RegistryClient,
    organisation: str,
    project: SeedProject,
    existing_names: set[str],
    dry_run: bool,
) -> None:
    """Create one project, unless a project of that name already exists.

    Projects are not updated when present. The Registry API's project update
    call replaces the API list wholesale, and silently rewriting a project a
    human may have adjusted is worse than leaving it alone and saying so.
    """
    target = f"{organisation}/{project.name}"

    if project.name in existing_names:
        log("SKIP", "project", target, "already exists")
        return

    if dry_run:
        log("WOULD", "project", target, f"create with {len(project.api_slugs)} APIs")
        return

    client.create_project(organisation, project.name, project.description, project.api_slugs)
    log("CREATE", "project", target, f"{len(project.api_slugs)} APIs")


def run_seed(client: RegistryClient, organisation: str, dry_run: bool) -> None:
    """Create every API, then every project.

    Order matters: the Registry API requires APIs to exist before a project can
    name them.
    """
    for api in SEED_APIS:
        seed_one_api(client, organisation, api, dry_run)

    existing_names = set()
    if not dry_run:
        existing_names = {project.get("name", "") for project in client.list_projects(organisation)}

    for project in SEED_PROJECTS:
        seed_one_project(client, organisation, project, existing_names, dry_run)


def run_teardown(client: RegistryClient, organisation: str, dry_run: bool) -> None:
    """Remove everything this utility creates, and nothing else.

    Only names declared in SEED_APIS and SEED_PROJECTS are touched. Anything
    else found in the organisation is left alone, however tempting it looks —
    this must never become a 'delete everything' button.
    """
    projects_by_name = {}
    if not dry_run:
        projects_by_name = {
            project.get("name", ""): project for project in client.list_projects(organisation)
        }

    for project in SEED_PROJECTS:
        target = f"{organisation}/{project.name}"
        if dry_run:
            log("WOULD", "project", target, "delete")
            continue
        found = projects_by_name.get(project.name)
        if not found:
            log("SKIP", "project", target, "not present")
            continue
        client.delete_project(organisation, str(found.get("id") or project.name))
        log("DELETE", "project", target)

    for api in SEED_APIS:
        target = f"{organisation}/{api.slug}"
        if dry_run:
            log("WOULD", "api", target, "delete")
            continue
        client.delete_api(organisation, api.slug)
        log("DELETE", "api", target)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the trial SwaggerHub organisation with test APIs and projects."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would happen without calling the API."
    )
    parser.add_argument(
        "--teardown", action="store_true", help="Delete the APIs and projects this utility created."
    )
    parser.add_argument(
        "--confirm", action="store_true", help="Required alongside --teardown. Guards against accidents."
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if arguments.teardown and not (arguments.confirm or arguments.dry_run):
        print("--teardown requires --confirm (or --dry-run to preview).", file=sys.stderr)
        return 1

    api_key, organisation = read_configuration()
    client = RegistryClient(api_key)

    mode = "teardown" if arguments.teardown else "seed"
    print(f"# {mode} against organisation {organisation}"
          f"{' (dry run — nothing will be called)' if arguments.dry_run else ''}\n")

    try:
        if arguments.teardown:
            run_teardown(client, organisation, arguments.dry_run)
        else:
            run_seed(client, organisation, arguments.dry_run)
    except (RegistryError, FileNotFoundError) as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1

    print("\n# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
