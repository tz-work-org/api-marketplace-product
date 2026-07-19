# Contributing — branching and review strategy

**Status:** agreed. Not yet applied — the repository has no commits and no remote.

**This repository is a proof of concept.** It is built on a personal machine, in a **public**
repository under a personal GitHub organisation, and is not the production repository. Production
will be created separately in the employer's GitHub account once this PoC is built, reviewed, and
approved.

It is public because branch protection is unavailable on private repositories under a free plan,
and demonstrating that the workflow gates actually work matters more here than keeping the code
closed. The material that should not be public — the build context document and the verification
reports — is held in `.local/` and never committed.

The PoC is therefore deliberately shaped like production, and this document records **where the
two differ and why** — so production is configured from a written delta rather than from memory.

---

## Why this repository is not branched like ordinary code

`main` is the **declared desired state of the portal**. CI runs `plan` on every pull request and
`apply` on merge (§A.13). The publisher reconciles what `main` says against what the portal
actually contains.

That makes long-lived parallel branches actively wrong here. A `develop` branch would be a second,
competing declaration of what the portal should contain — and the reconciler can only converge
toward one. The moment the two disagree, "what should the portal look like?" has no answer.
GitFlow solves a problem this repository does not have.

**Therefore: trunk-based development, protected `main`, short-lived branches.**

---

## Branch types

Two classes of change flow through this repository with different reviewers — the intake form
governs a product's existence, the repository governs its content (§B.3). Branch prefixes make
that split visible.

| Prefix | For | Reviewer | Example |
|---|---|---|---|
| `request/APR-####` | Product content from an approved intake request | CODEOWNERS (product owner) | `request/APR-0042` |
| `feat/<slug>` | Publisher code — new capability | Platform team | `feat/reconciler-diff` |
| `fix/<slug>` | Publisher code — defect | Platform team | `fix/toc-pagination` |
| `docs/<slug>` | Reports, ADRs, README | Platform team | `docs/a2-verification` |
| `seed/<slug>` | Fixture utility (§A.16) | Platform team | `seed/claims-core-specs` |

`request/{requestId}` is not an arbitrary choice. **§B.11 specifies exactly that branch name** for
the deferred `mutate` command. Using it manually now means `mutate` later automates a convention
that already exists rather than imposing a new one — the same reasoning behind §B.11's instruction
to adopt the PR title convention from day one.

Branches are short-lived and deleted on merge.

---

## Pull request titles

The title is not decoration. §B.12 generates the changelog from merged PR titles, and those titles
carry request IDs — which is what closes the loop back to SharePoint. §B.11 calls the request ID
in the PR title "the only join between SharePoint and Git," and says to adopt it from day one.

| Change | Format | Example |
|---|---|---|
| Request-driven | `APR-####: <what changed>` | `APR-0042: create Claims product` |
| Publisher code | `<type>: <what changed>` | `feat: add slug-scoped ToC matching` |

---

## Merge strategy: squash only

Every pull request becomes exactly one commit on `main`, carrying the PR title.

Three reasons, in order of weight:

1. **`git log --oneline` becomes the changelog.** The SharePoint join is preserved in history
   rather than reconstructed from merge commits.
2. **A bad `apply` bisects to a single commit.** With merge commits, a broken portal state could
   originate anywhere inside a merged branch.
3. Linear history is readable by people who are not git specialists — consistent with §A.12's
   principle that the governance architect must be able to follow the work.

---

## Protection rules on `main`

Branch protection is two different kinds of rule, and only one kind works with a single author.

**Mechanical gates** — no dependency on how many people exist. All enabled in the PoC.
**Human gates** — require a second person. GitHub will not let an author approve their own pull
request, so these are the only settings that would actually block solo work.

| Rule | Kind | PoC | Production |
|---|---|---|---|
| No direct pushes to `main` | mechanical | ✅ on | ✅ on |
| Pull request required | mechanical | ✅ on | ✅ on |
| Required status checks (below) | mechanical | ✅ on | ✅ on |
| Linear history (enforces squash) | mechanical | ✅ on | ✅ on |
| No force-push, no branch deletion | mechanical | ✅ on | ✅ on |
| Branches auto-delete on merge | mechanical | ✅ on | ✅ on |
| **Required approvals** | human | **0** | **≥ 1** |
| **Required CODEOWNERS review** | human | **off** | **✅ on** |

Required status checks, identical in both:

- manifest schema validation
- `reconciler.py` unit tests (no network — §A.14 criterion 8)
- `plan` runs clean
- CODEOWNERS staleness check (§A.10)

**The entire PoC-to-production delta is the two bolded rows.** The workflow is otherwise
identical: branch, pull request, CI, squash merge. A solo author merges their own pull request
once checks are green, which exercises every mechanical gate without ever being blocked by one.

In production, required CODEOWNERS review is what gives §A.10 teeth — without enforcement,
generating a `CODEOWNERS` file is decoration.

---

## Protection settings are not in the repository

Rulesets are GitHub account configuration. `git push` carries **files**, not protection rules —
so none of the table above transfers to the production repository by cloning or pushing.

To stop production being configured from memory, the ruleset is exported to a version-controlled
file once applied:

```
.github/rulesets/main.json
```

GitHub rulesets export and import as JSON through `gh api`, so production applies its protection
with one command and differs from this file by exactly the two human-gate fields. The delta
becomes reviewable rather than tribal knowledge.

> **Not yet written.** The ruleset JSON is exported from GitHub after the rules are applied — it
> is not hand-authored. Inventing the payload shape would repeat the mistake §A.2 exists to
> prevent, so this file appears only once there is a real ruleset to export.

---

## What the PoC proves, and what it cannot

State this plainly when presenting for approval. Overclaiming here is the easiest way to lose
credibility on everything else in the report.

**Genuinely proven by a single-author PoC:**

- the reconciler converges — two consecutive `apply` runs, second plan empty (§A.14 criterion 2)
- `plan`, `apply`, and `publish` behave as specified
- prune guardrails fire, including `--max-deletes`
- the manifest schema rejects malformed input at `plan`
- CI runs `plan` on pull request and `apply` on merge
- the `CODEOWNERS` **generator** works, and its staleness check fails a build

**Not provable with one person, and low-risk because each is one setting:**

- that required-reviewer enforcement actually gates a merge
- that a product owner reviewing their own folder catches anything
- the §B.3 two-audience split — platform team versus product owner — which needs two audiences

With one author, `CODEOWNERS` maps every product to the same person, so it is trivially
satisfied. **The generator is exercised; the enforcement is not.** Say so rather than letting a
reviewer assume ownership was tested end to end.

---

## Repository access and transfer

The repository is **private**, so every clone must be authenticated — a public clone needs no
credentials, a private one always does. This is the whole difference, and the usual cause of a
clone failing on a locked-down machine.

**Preferred method: a fine-grained personal access token over HTTPS.** HTTPS runs on port 443,
which corporate firewalls almost always permit, and it requires nothing to be installed.

Scope the token to **this repository only**, `Contents: Read and write` plus `Metadata: Read`,
with an expiry. Scoping matters: if the work machine is imaged or access is later withdrawn, the
exposure is one PoC repository and the token is revoked in a click. A classic (non-fine-grained)
token would grant access to the entire account — do not use one here.

Fallbacks, in order of preference:

| Situation | Fix |
|---|---|
| GitHub CLI installable | `gh auth login` — least friction, but authenticates the whole account |
| Want the narrowest possible access | Deploy key — SSH keypair valid for this repository alone |
| SSH times out on port 22 (firewall) | GitHub serves SSH on 443: set `Hostname ssh.github.com`, `Port 443` in `~/.ssh/config` |
| `SSL certificate problem` on clone | Corporate TLS interception — needs the corporate root CA installed |

Tokens are never committed, never pasted into issues or pull requests, and never shared through
this or any other chat transcript.

---

## Tags — not yet, but the model allows it

§B.12's release cadence is **unblocked**: the §A.2 verification confirmed ToC deletions are
soft-deleted "up until the time of publish," so draft and live are genuinely separable.

If a cadence is adopted later — *merge converges the draft, a release publishes* — it is a tag
(`release/2026-07-19`) triggering `publish` across changed products. **No branching change
required.** That is precisely why the trunk model is worth getting right now.

---

## Two phases

**Build phase (current).** Only `feat/`, `fix/`, `docs/`, and `seed/` branches. One author, no
products, no intake. The strategy costs almost nothing and establishes the habit.

**Operating phase.** `request/` branches appear, product owners review their own folders, and
CODEOWNERS enforcement starts carrying real weight.

---

## Bootstrap

The repository currently has **zero commits**, so there is no `main` to branch from and nothing to
protect. The unavoidable sequence:

1. One bootstrap commit directly on `main`: context document, `.gitignore`, `.env.example`, this
   file, and both verification reports.
2. Create the GitHub repository **as private** and push.
3. Apply protection rules.
4. Export them to `.github/rulesets/main.json` and add that file via a pull request.
5. Everything thereafter goes through a pull request.

**Step 1 is the only commit that will ever land directly on `main`.**

"Export" here means reading the live configuration out of GitHub and saving it to disk —
`gh api repos/{owner}/{repo}/rulesets > .github/rulesets/main.json` — not a git operation.

---

## Working agreement

- Never commit `.env`. It is gitignored; keep it that way.
- **This repository is public.** Nothing identifying the organisation, its API estate, or the
  governance programme belongs in it. Before committing, assume anyone can read it.
- `.local/` is gitignored and holds the author's working material — the build context document and
  the §A.2 / §A.16 verification reports. It stays on the author's machine and is **not** part of
  the repository.
- Trial-account identifiers (organisation name, portal subdomain, portal UUID) are configuration,
  not content. They live in `.env`, never in committed files.

> **Consequence to carry into review:** §A.14 acceptance criterion 1 is *"the verification report
> from §A.2 exists."* The reports exist and both gates were cleared, but they are held outside the
> repository, so criterion 1 must be evidenced separately rather than by pointing at a file. If
> that proves awkward at review, the fix is to commit the reports with the three identifiers
> scrubbed — a single pull request.
