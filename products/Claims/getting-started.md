# Getting started with the Claims APIs

The Claims product groups the APIs used to register a claim, track it through
assessment, and settle it.

## What is here

| API | Use it to |
|---|---|
| Claims Intake | Register a first notification of loss and amend an open claim |
| Settlement | Raise and amend payment instructions against an approved claim |

These two APIs come from different Swagger Studio projects — Claims Intake from
`claims-core`, Settlement from `claims-payments`. A product groups APIs by how
consumers use them, not by where they are maintained.

## Before you start

You will need a client credential for the environment you are targeting. Claims
data is subject to retention rules, so do not copy claim payloads into tickets,
logs, or test fixtures.

## A typical flow

1. Register the claim with `POST /claims`. The claim reference is allocated by
   the service — do not invent one.
2. Track progress through the claim's status.
3. Once approved, raise a settlement with `POST /settlements`. It is created in
   `pending-approval`; a second party releases it.

## Getting help

Raise an issue against this repository, or contact the product owner listed in
`manifest.json`.
