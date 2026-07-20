# ADR-0002: Removal semantics — orphans shown, pruning opt-in

**Status:** Accepted
**Date:** 19 July 2026
**Deciders:** Governance architect (author)

## Context

The reconciler compares the whole repository (desired state) against the whole
portal (actual state) on every run — there is no state file, because live state
is fully readable (§A.6). That leaves one question every declarative tool must
answer: **when something is removed from the source, what happens to the live
resource it used to describe?**

The three tools in this space answer it three different ways, and the choice has
real safety consequences.

### The spectrum

| Tool | Removal from source | Failure mode |
|---|---|---|
| **Terraform** | destroys the resource (state-tracked) | a careless `rm` in config can delete production; blast radius is the whole plan |
| **ARM / Bicep, incremental mode** (the default) | **silently ignored** — the resource stays in Azure | drift accumulates invisibly; you never learn the orphan exists |
| **ARM / Bicep, complete mode** | deletes resource-group resources absent from the template | reasons at resource-group scope and does not reliably prune nested **child** resources (APIM operations, policies, products), so it is both too blunt at the top and too weak underneath |

The **Bicep-on-APIM** case is the one directly relevant to this project, because
a colleague's APIM IaC repository is the closest sibling to what we are building:
Bicep defining APIs, operations, policies, products and diagnostics. Its observed
behaviour was exactly incremental mode's — **remove an operation from the Bicep
file and it remains in APIM**, indefinitely and without warning. Most of what one
cares about in APIM (operations, policies, products) are child resources, which
even complete mode does not cleanly prune, so the drift is not easily escaped by
switching modes.

This is not a Bicep defect so much as the ARM deployment model showing through:
incremental mode is the safe default precisely because complete mode is
dangerous. The cost of that safety is silence.

CLAUDE.md already frames this tool's analogy as **Azure APIOps, not Bicep** —
APIOps exists to paper over exactly these gaps for APIM. Notably, APIOps decides
deletions from a **git commit diff** (which artifacts disappeared between
commits); we diff against **live state** instead. Live-state diffing needs no
state file and self-heals against out-of-band portal edits, at the cost of a full
read each run — the right trade for a portal whose state is fully readable.

## Decision

**Removal is surfaced, never silently actioned, and destructive pruning is
opt-in and bounded.**

1. **A removed resource becomes an `orphan` operation, shown on every `plan`.**
   With pruning off (the default) nothing acts on it, but the reconciler names it
   each run. This is the deliberate inverse of Bicep incremental's silence: the
   default is safe *and* visible, not safe *because* blind.
2. **Deletion is opt-in via `--prune`,** which turns table-of-contents orphans
   into `delete` operations (§A.8).
3. **`--max-deletes` bounds the blast radius.** A plan wanting more deletions than
   the threshold fails the whole run before anything executes (§A.8) — the
   guardrail Terraform's model lacks.
4. **Product-level deletion is never performed.** A product present in the portal
   but absent from the repository is always an orphan; retirement is
   `hidden: true`, not removal (§A.8). Product deletion is out of MVP1 and its
   soft-vs-hard semantics are still unverified.

## Options considered

### Option A: Prune by default (Terraform-shaped)

Removing from the repo deletes from the portal on the next `apply`.

**Pros:** the repository is a true mirror; no orphans ever linger.
**Cons:** one careless deletion of a manifest, or a mis-generated slug (§A.7,
where a slug change reads as *create new + orphan old*), quietly destroys live
pages. The reconciler runs against the **whole org's** products at once, so the
blast radius is every product in one go. Rejected: too much destructive power
behind too small a mistake.

### Option B: Never delete (Bicep incremental-shaped)

Show nothing; leave every removed resource in place forever.

**Pros:** trivially safe — nothing is ever destroyed.
**Cons:** this is the exact failure we watched happen on APIM. Drift accumulates
with no signal; the portal fills with pages no manifest describes, and the only
way to find them is to read the portal by hand. A reconciler that cannot even
*report* divergence is not reconciling. Rejected.

### Option C: Orphans shown by default, pruning opt-in and bounded *(chosen)*

**Pros:** the safe default is also the visible one — drift is named every run,
deletion requires an explicit flag, and `--max-deletes` caps how much a single
run can remove. Product deletion is excluded entirely, so the highest-value
resources cannot be lost to this tool at all.
**Cons:** orphans do not clean themselves up; someone must choose to `--prune`.
That is a feature here — the choice to destroy should be conscious — but it does
mean the portal can hold known, reported orphans indefinitely.

## Trade-off analysis

The real choice is between A and C: whether removal should *destroy by default* or
*report by default*. Both avoid Option B's blindness. A is a true mirror but
concentrates destructive power behind ordinary edits, over the whole org at once.
C keeps the same visibility while making destruction a deliberate, bounded act.

For a governance tool whose primary reader is not a Python specialist (§A.12), the
property that matters is that **nothing irreversible happens without someone
having asked for it and seen it first**. C delivers that; A does not. The residual
cost of C — orphans that linger until pruned — is acceptable because they are
never silent: the plan names them every time.

## Consequences

**Easier**

- Drift is auditable: `plan` is a complete divergence report even when it will
  change nothing, which is what a governance review actually wants to read.
- The blast radius of a mistake is bounded by `--max-deletes` and floored by the
  product-deletion exclusion — a bad run cannot lose a whole product.

**Harder**

- Orphans persist until a human runs `--prune`; the portal can carry known,
  reported cruft. Accepted as the cost of never deleting by surprise.
- `--max-deletes` has a default (§A.8) that is right for three products and must
  be revisited as real product counts grow — the same scaling caveat ADR-0001
  raises for Lambda's execution cap.

**To revisit**

- **Product pruning.** Excluded from MVP1, but once product-deletion semantics are
  verified (soft vs hard), decide whether a heavily-guarded product `--prune`
  belongs in a later version or stays permanently a `hidden` retirement.
- **Per-product scoping.** `plan`/`apply` run against the whole `products/` root,
  so every undeclared portal product is an orphan. If the repo ever stops being
  the single source of truth for the entire org, orphan semantics would need a
  `--product` filter to avoid reporting deliberately-external products as drift.
