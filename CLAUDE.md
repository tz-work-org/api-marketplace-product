# Swagger Portal product publisher

A Python CLI that reconciles product manifests held in this repository against a live Swagger
Portal. The Portal API offers only one-instruction-at-a-time CRUD — no plan, no diff, no pruning,
no Terraform provider. **This tool is that missing reconciler.** The closest analogy is Azure
APIOps, not Bicep; no state file is needed because live state is fully readable.

**This repository is a proof of concept.** Production will be created separately in the employer's
GitHub organisation once this is built, reviewed, and approved.

## The build specification lives outside this repo

`.local/` is gitignored and holds the material this project is built from:

| File | What it is |
|---|---|
| `.local/swagger-portal-product-publisher-context.md` | The build specification. Part A is the spec; **Part B is standing context and must not be implemented.** |
| `.local/verification/portal-api-verification.md` | §A.2 gate — Portal API endpoints reconciled against the spec |
| `.local/verification/registry-api-verification.md` | §A.16 gate — Registry API, for the seed utility |

**Read the context document before making design decisions.** Section references throughout this
repo (§A.7, §A.12, §B.10) point into it. It is not committed because it carries organisational
programme detail and this repository is public.

## Non-negotiables

**§A.12 — readability is the primary acceptance criterion.** The governance architect, not a
Python specialist, must be able to read the code and follow it. Where readability and
sophistication conflict, choose readability. No Clean Architecture layers, no dependency
injection, no abstract base classes with one implementation, no metaprogramming, no custom
decorators, no inheritance. Prefer an obvious twelve-line function to a clever four-line one.

**§A.7 — slug is identity.** For products and table-of-contents entries alike. Title is a mutable
attribute; renaming updates in place. Changing a slug declares a new resource and orphans the old
one. Entry slugs are scoped to their parent, not global. Getting this wrong silently duplicates
pages, which is what the reference implementation does.

**§A.12 — the one architectural rule.** `reconciler.py` must never import `portal_client`. No I/O,
no HTTP, no filesystem. This makes the diff testable without a network — and since the author's
work laptop cannot reach the SmartBear APIs at all, it is what keeps that machine productive.

**ADR-0001 — three rules for runner portability.** The same code runs under GitHub Actions,
Jenkins, and Lambda:

1. No `sys.exit()` outside the entry point — core functions return or raise
2. No `os.environ` access outside one configuration function
3. No printing of decisions from inside the logic

**Do not invent endpoint shapes.** Both API gates were cleared by retrieving the real OpenAPI
descriptions and reconciling against them. If an endpoint is needed that has not been verified,
verify it first — that discipline is why §A.2 exists.

## Build order (§A.15) — where things stand

| Step | | |
|---|---|---|
| 1 | Verification gate (§A.2) | ✅ done |
| 2 | Seed fixture utility (§A.16) | ✅ done |
| 3 | `models.py`, `manifest.py` | ✅ done |
| 4 | `portal_client.py`, read-only first | **next** |
| 5 | `reconciler.py` plus unit tests | |
| 6 | `cli.py` with `plan` only | |
| 7 | `executor.py` and `apply` | |
| 8 | `publish` | |
| 9 | Deletions and prune guardrails — last, because destructive | |
| 10 | `codeowners.py` | |
| 11 | CI wrappers | |

Check `git log --oneline` and `gh pr list --state all` for what actually landed.

## Environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # pulls in runtime too
set -a; source .env; set +a                     # never commit .env
```

`.env` needs `SWAGGERHUB_API_KEY`, `PORTAL_SUBDOMAIN`, `SWAGGERHUB_ORG`. See `.env.example`.

```bash
.venv/bin/python -m publisher validate    # load and print every manifest
.venv/bin/python -m pytest tests/ -q      # no network needed
.venv/bin/python seed/seed.py --dry-run   # fixture utility, needs network
```

Use `.venv/bin/python`, not a bare `python3` — the system interpreter has no `requests`.

## Conventions

- **Dependencies:** runtime is `requests` + `jsonschema` (`requirements.txt`, ships to Lambda);
  development is `pytest` (`requirements-dev.txt`, does not ship). **Nothing else without
  asking** — present options and a recommendation rather than choosing silently.
- **Python:** floor 3.11, target line 3.13 (`.python-version`), matching the `python3.13` Lambda
  runtime. The patch is not pinned because Lambda cannot select one.
- **Branching:** trunk-based, protected `main`, squash merge. `feat/`, `fix/`, `docs/`, `seed/`,
  and `request/APR-####` for intake-driven work. See `CONTRIBUTING.md`.
- **Logging:** one line per operation — `CREATE toc-entry claims/getting-started`.
- **Errors:** fail fast, name the product and entry involved. **No rollback or compensating
  transactions** — a reconciler is safe because re-running converges.

## Known unverified

- The `contentUrl` form for API references — `api.swaggerhub.com/apis/{org}/{api}/{version}/swagger.json`
  is used, from the Portal API's own schema example; §A.5 shows an `app.swaggerhub.com` form.
  Untested until `apply` exists.
- Attachment upload endpoints **do not exist** in the Portal API specification. Logo upload is
  deferred; `logo`/`logoDark` stay in the manifest schema unimplemented.
- Whether product deletion is soft or hard. Excluded from MVP1 regardless (§A.8).
- A documented upsert may not be an upsert in every state — a published SwaggerHub API cannot be
  overwritten, and `force=true` does not override it. Expect similar surprises where the publisher
  meets already-published portal content.
