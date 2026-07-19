# Seed fixture utility

Creates realistic APIs and projects in a trial SwaggerHub organisation, so the
product publisher has something to reconcile against.

**This is a fixture tool, not part of the publisher (§A.16).** No publisher
module imports it, it has its own entry point, and the publisher must remain
buildable and testable with this directory absent.

## Why it exists early

The publisher cannot be exercised without products to reconcile, and products
cannot exist without published APIs to reference. §A.15 therefore places this
before `models.py`.

## Usage

```bash
set -a; source .env; set +a          # SWAGGERHUB_API_KEY, SWAGGERHUB_ORG

python3 seed/seed.py --dry-run       # print what would happen, call nothing
python3 seed/seed.py                 # create and publish
python3 seed/seed.py --teardown --confirm
```

Teardown only removes names declared in `SEED_APIS` and `SEED_PROJECTS`. Anything
else in the organisation is left alone.

## What it creates

Four projects, nine APIs, insurance-flavoured. `Claims` deliberately spans two
projects, so that a portal product drawing APIs from more than one project can
be tested (§A.5, §B.10 scenario 1).

| Project | APIs |
|---|---|
| `claims-core` | Claims Intake, Claims Search, Claims Status |
| `claims-payments` | Settlement, Recovery |
| `policy-admin` | Policy Issuance, Policy Endorsement |
| `party-services` | Party Search, **Party Contact** |

Each specification centres on a genuinely different resource — a claim, a query,
a state machine, a payment instruction, a subrogation case, a policy, an
adjustment, an identity match, a consent record. §A.16 requires that a reviewer
skimming two of them can immediately see they describe different things, so they
are not one template with the noun swapped.

**Party Contact is left unpublished on purpose.** It exists so the publisher can
be shown failing clearly — rather than confusingly — when a manifest references
an API that cannot be linked to a portal product. `party-services` was chosen
because it is peripheral to the §B.10 walkthrough scenarios; leaving Recovery
unpublished would have broken scenario 7, which needs it live and then removed.

## Idempotency, and the surprise in it

Re-running is safe: no duplicates, no failures.

Getting there was not free, and the §A.16 verification report originally said it
would be. The Registry API documents `POST /apis/{owner}/{api}` as *"create a new
API or update an existing API"*, which reads like an unconditional upsert. It is
not. Once an API is published, the same call returns:

```
409 {"error":"Published APIs can not be overwritten"}
```

The `force=true` query parameter does **not** override this — that was tested
directly. So `seed_one_api` unpublishes an existing published API, writes it, and
publishes it again. The log makes each step visible rather than hiding the
dance:

```
UNPUBLISH api sparklayerinc-55d/claims-intake-api  (temporarily — published APIs cannot be overwritten)
UPDATE    api sparklayerinc-55d/claims-intake-api  (1.0.0)
PUBLISH   api sparklayerinc-55d/claims-intake-api  (1.0.0)
```

Worth remembering when the publisher's own update paths are built: **a
documented upsert may not be an upsert in every state.**

## Projects are created but never updated

If a project already exists it is skipped, not reconciled. The Registry API's
project update call replaces the API list wholesale, and silently rewriting a
project a human may have adjusted is worse than leaving it alone and saying so.

This utility is a fixture, not a reconciler. The publisher is the reconciler.

## Specifications live as files

`seed/specs/*.yaml`, per §A.16 — reviewable, diffable, and hand-editable when a
governance rule needs satisfying, rather than generated inline at push time.

They are posted to SwaggerHub as raw YAML text rather than parsed, which is what
keeps PyYAML outside the §A.12 dependency budget. API names and versions are
declared explicitly in `SEED_APIS` instead of being read out of the files,
because the Registry API matches API names case-sensitively when they are listed
in a project, and deriving the same name in two places is how that goes wrong.

## Governance is not in play

The target is a trial organisation with governance disabled and no rulesets
configured, so specifications publish without passing any standardisation check
and no `x-owner` or naming conventions are required. No ruleset validation is
built into this utility, per §A.16.

The consequence to remember: this fixture exercises the publisher, not the
conformance gate. The "non-conformant API is unselectable" behaviour in the
intake form cannot be tested here.
