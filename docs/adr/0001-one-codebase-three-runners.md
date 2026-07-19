# ADR-0001: One codebase, three runners

**Status:** Accepted
**Date:** 19 July 2026
**Deciders:** Governance architect (author), platform team at production review

## Context

The publisher must be demonstrable regardless of which execution environment is
available on the day.

- The organisation intends to move from **Jenkins to GitHub Actions**, but the
  GitHub Actions infrastructure is not ready.
- **AWS Lambda can reach the SmartBear APIs** — verified directly.
- **Jenkins is believed to allow the same egress**, verified by a colleague, not
  first-hand.
- The **author's work laptop cannot reach the SmartBear APIs at all** due to
  workstation restrictions.

A demonstration must not be blocked because one environment is unavailable. That
makes the runner a deployment detail, not an architectural one.

§A.13 already establishes the principle for two runners: *"Two thin wrappers,
both shelling out to the same CLI. No logic in either."* This ADR extends that
to a third and states the rules that keep it true.

### What differs between the three

| | GitHub Actions | Jenkins | Lambda |
|---|---|---|---|
| Invocation | shell | shell | `handler(event, context)` |
| Result signal | exit code | exit code | returned value |
| Output | stdout | stdout | stdout → CloudWatch |
| Configuration | Actions secrets | credentials binding | environment variables |
| **Repository on disk** | checkout | checkout | **nothing checked out** |

The first four are trivial. The last is the real problem: the publisher reads
manifests from a working tree, and Lambda has none.

## Decision

**One core, three thin entry points, and three rules that keep the core free of
runner knowledge.**

The core (`models`, `manifest`, `portal_client`, `reconciler`, `executor`,
`codeowners`) knows nothing about how it was invoked. Entry points translate.

### The three rules

1. **No `sys.exit()` outside the entry point.** Core functions return results or
   raise. §A.9's exit codes (`0` converged, `2` changes pending, `1` error) are a
   *CLI* concern; Lambda returns the same underlying value as a dict. A function
   that exits the process removes the caller's ability to decide.
2. **No `os.environ` access outside one configuration function.** Read once, pass
   the values in. Otherwise supporting a new configuration source means hunting
   through modules.
3. **No printing of decisions from inside the logic.** The reconciler returns an
   operation list; the entry point formats it. `print()` reaches stdout under all
   three runners, so the §A.12 logging style carries over unchanged.

### How Lambda gets its manifests

The Lambda handler **downloads the repository tarball from the GitHub API into
`/tmp`**, unpacks it, and passes that directory to the same function the CLI
calls.

This is a parameter, not an abstraction. `manifest.py` takes a `Path`; the caller
is responsible for producing one. **No `ManifestSource` interface, no provider,
no adapter** — §A.12 forbids abstract base classes with a single implementation
and forbids premature abstraction, and a function taking a `Path` satisfies all
three runners without any indirection.

## Options considered

### Option A: CLI only, shell wrappers (the specification as written)

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | None |
| Portability | **Fails the requirement** |
| Team familiarity | High |

**Pros:** exactly what §A.13 describes; nothing new to explain.
**Cons:** Lambda cannot shell out to a CLI against a repository that was never
checked out. Rules out the one environment verified to reach SmartBear.

### Option B: One core, thin entry point per runner *(chosen)*

| Dimension | Assessment |
|---|---|
| Complexity | Low — one extra file, roughly 30 lines |
| Cost | Lambda packaging step (layer or zip) |
| Portability | All three runners, no conditional code |
| Team familiarity | High — same shape as §A.13's existing wrappers |

**Pros:** the demonstration is never blocked by infrastructure; the same `plan`
is provably the same code in all three places; extends a principle the
specification already states rather than introducing a new one.
**Cons:** adds `lambda_handler.py`, which is not in §A.12's module table (see
Consequences); Lambda needs dependencies bundled.

### Option C: Runner abstraction layer

An `ExecutionEnvironment` interface with three implementations, selected at
startup.

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Cost | Ongoing — every new feature crosses the abstraction |
| Portability | Good |
| Team familiarity | Low |

**Pros:** textbook-shaped; runners become pluggable.
**Cons:** **directly violates §A.12** — abstract base classes, ports and adapters,
extension points for hypothetical needs. It would put three layers of indirection
between a reader and the work in order to solve a problem that a function
parameter already solves. §A.12's acceptance criterion is that a non-specialist
can follow the code; this option fails it.

### Option D: A separate codebase per runner

| Dimension | Assessment |
|---|---|
| Complexity | Low per copy, high in aggregate |
| Cost | Triple maintenance |
| Portability | Nominal |
| Team familiarity | High |

**Pros:** each copy is trivially simple.
**Cons:** three copies diverge. A demonstration would then prove that *one* copy
works, which is not the claim being made.

## Trade-off analysis

The real choice is between **B and C**, and it is the same trade-off §A.12 already
adjudicates: whether to solve variation with an abstraction or with a parameter.

Option C is what a Python specialist would reach for by habit, and it would work.
It fails on the criterion the specification treats as primary — that the
governance architect, not a Python specialist, can read the code and follow it.
Three runner implementations behind an interface means the reader must hold the
interface, the selection logic, and one implementation in mind simultaneously to
answer "what happens when I run `plan`?"

Under Option B the answer is: read `cli.py`, or read `lambda_handler.py`. Each is
short and neither hides anything.

The cost of Option B is honest and bounded: one extra file, and a rule about
`sys.exit` that must be observed rather than enforced by a type system. That is a
review concern, not an architecture concern.

## Consequences

**Easier**

- Demonstrations run wherever infrastructure happens to be available.
- The work laptop stays productive despite no SmartBear egress: `reconciler.py`
  has no I/O by §A.12's rule, so the entire diff engine is developable and
  testable behind the firewall. That rule now carries weight its author did not
  anticipate.
- Migrating Jenkins → GitHub Actions becomes a wrapper swap, not a port.

**Harder**

- `lambda_handler.py` is a **deviation from §A.12's module table**, which the
  specification presents as fixed. It is justified by §A.13's existing wrapper
  principle and contains no logic, but it is a conscious departure and should be
  raised at production review rather than discovered in a diff.
- Lambda requires `requests` bundled as a layer or zip — a packaging step CI does
  not need.
- Three rules must be observed by reviewers; nothing enforces them mechanically.

**To revisit**

- **Lambda's 15-minute execution cap.** Irrelevant at three products. Across ~80
  products with a per-product reconcile, it becomes a real limit. Decide before
  anyone proposes Lambda for production rather than demonstration.
- **Whether Lambda should reconcile at all in production.** The publisher's
  natural trigger is a Git merge, which is a CI event. Lambda earns its place here
  because it is the environment verified to work, not because it is the right
  long-term home.
- `/tmp` is capped at 512 MB by default. Ample for manifests and markdown; worth
  remembering if documentation images are ever fetched.

## Action items

1. [x] Remove `SystemExit` from `seed.py`'s configuration reader — rule 1 was
       already violated in existing code.
2. [ ] Apply the three rules from the first line of `models.py` onward.
3. [ ] Write `cli.py` with §A.9's exit codes as the only place they appear.
4. [ ] Write `lambda_handler.py` — config, tarball into `/tmp`, call, return dict.
5. [ ] Add a `requirements.txt` install step to both CI wrappers, so `python3`
       resolves to an interpreter that actually has the dependencies.
7. [ ] Keep all four environments on the same Python line — `.python-version`
       says `3.13`, CI must set `python-version: "3.13"`, and the Lambda runtime
       must be `python3.13`. A mismatch fails somewhere other than where it was
       tested, which is the failure mode this ADR exists to prevent.
6. [ ] Raise the §A.12 module-table deviation at production review.
