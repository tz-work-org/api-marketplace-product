# Branch protection rulesets

`main.json` is the ruleset applied to `main` in this repository, exported from GitHub with
server-generated fields (`id`, timestamps, `_links`) removed so it can be applied directly.

Protection rules are GitHub configuration, not repository content — `git push` does not carry
them. This file exists so the production repository is configured from a reviewed artefact rather
than from memory.

## Applying it

```bash
gh api -X POST repos/{owner}/{repo}/rulesets --input .github/rulesets/main.json
```

Re-export after any change, so the file and the live configuration do not drift:

```bash
gh api repos/{owner}/{repo}/rulesets/{id} > /tmp/rs.json   # then strip generated fields
```

## PoC versus production

This file is the **PoC** configuration. Production differs in exactly two fields, both under the
`pull_request` rule — the two that require a second person and therefore cannot be exercised by a
single author:

| Field | PoC | Production |
|---|---|---|
| `required_approving_review_count` | `0` | `1` or more |
| `require_code_owner_review` | `false` | `true` |

Everything else — no direct pushes, no force-push, no branch deletion, linear history, squash-only
merges — is identical and is enforced here.

## Not yet included: required status checks

There is deliberately no `required_status_checks` rule. CI does not exist yet, and a required
check that never reports leaves every pull request permanently pending and unmergeable.

Add it in the same pull request that introduces the workflows (§A.13), with contexts for manifest
schema validation, `reconciler.py` unit tests, `plan`, and the `CODEOWNERS` staleness check.
