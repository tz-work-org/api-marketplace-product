# ADR-0003: Reliability — correctness by construction, robustness deferred

**Status:** Accepted
**Date:** 19 July 2026
**Deciders:** Governance architect (author)

## Context

The publisher is validated end to end for one product. Before it is trusted for
a handful — a realistic MVP1 portal is a small number of products, not hundreds —
the question "will it work reliably?" has to be answered honestly. It splits into
two very different claims:

- **Correctness:** does reconciling and applying N products produce the right
  result?
- **Operational robustness:** does a run survive the things that go wrong in
  practice — a transient network error, one bad product among several?

The first is a property of the design. The second is a set of engineering
features that the design deliberately does not have yet. Conflating them would
either overstate readiness or block the build on robustness the MVP does not need.

## Decision

**Rely on correctness by construction for a handful of products now, and defer
operational robustness to the "re-running converges" property, with the specific
gaps named and tracked rather than silently accepted.**

### Correctness holds for N products — why

Nothing in the pipeline carries a single-product assumption; every stage iterates
products and keys its state per product:

- **Load** (`load_all_products`) walks every product directory, validates each
  against the schema, and rejects duplicate product slugs. Products are
  independent `Product` objects.
- **Actual state** lists all portal products (paginated, following every page)
  and fetches the full tree only for the products the repo declares.
- **Reconcile** is pure and matches by slug; each product converges
  independently, and order and identity are scoped per product (§A.7).
- **Executor** threads portal ids in `_KnownIds`, keyed by product slug and by
  `(product_slug, entry_slug)` — correctly scoped, so one product's entries never
  resolve against another's ids. Operations arrive grouped by product.
- **Publish** is product-scoped: each product's own draft-vs-live diff drives its
  own publish.

This is validated by unit tests (multi-product reconcile) and, for this ADR, a
live five-product run (four creates plus one converged/updated product in a single
`apply`, then `publish`). Correctness for a handful is therefore **assured**, not
hoped for.

### The gaps — deferred, with dispositions

| # | Gap | Behaviour today | Disposition |
|---|---|---|---|
| 1 | **No retry/backoff on transient failures** | A 503, timeout or blip raises `PortalError` and stops the run | Mitigated by re-running (converges). Revisit for unattended CI. **Tracked as an issue.** |
| 2 | **Fail-fast halts the whole batch** | The first hard error stops every product after it in that run | Safe (re-run finishes the rest) but one poison product blocks the others. **Tracked as an issue.** |
| 3 | **Removals not handled** | Deleting an entry/product from a manifest shows an orphan, changes nothing | By design until §A.15 step 9 (prune). Already in the build order. |
| 4 | **Slug rename = create + orphan** | Changing a slug creates a new resource and orphans the old (§A.7) | Intentional — identity is slug. The plan shows it; documented, not fixed. |

## Options considered

### Option A: Build retry and per-product isolation now

**Pros:** a single run survives transient errors and bad products.
**Cons:** adds error-handling machinery — retry policy, backoff, per-product
error collection — to a codebase whose primary acceptance criterion is that a
non-specialist can read it (§A.12). It solves a problem a handful of products run
by a person does not yet have; the converge-on-rerun property already makes every
partial run safe. Premature for MVP1.

### Option B: Correctness now, robustness deferred and tracked *(chosen)*

**Pros:** ships the capability the MVP needs, keeps the code readable, and turns
the gaps from unknowns into named, tracked work. The reconciler's idempotence
means nothing is lost to a partial run in the meantime.
**Cons:** an unattended run can fail on a hiccup and need a re-run; one bad
product blocks others in a run. Both are acceptable at handful scale, run by a
person.

### Option C: Say nothing and treat "it works for one" as "it works"

Rejected. The single-product run did not exercise the batch paths, and silence
about the robustness gaps would be the kind of overclaim §A.2's discipline exists
to prevent.

## Consequences

**Easier**

- A handful of products can be reconciled, applied and published today, with
  confidence in the result.
- The gaps are legible: a reviewer sees exactly what is and is not handled, and
  the deferred work is tracked, not folklore.

**Harder**

- Until items 1 and 2 are built, reliable *unattended* operation (CI over many
  products) is not claimed — only reliable *attended* operation for a handful.

**To revisit — the trigger is scale and automation, not a number**

- When the publisher moves to unattended CI, or the product count grows past a
  handful, build retry/backoff (item 1) and per-product error isolation (item 2).
  These arrive with the same scaling pressures ADR-0001 already flags (Lambda's
  15-minute cap, per-product reconcile cost) and the per-product scoping note in
  ADR-0002.
