# Authoring products — a guide for the platform team

This repository **is the desired state of the Swagger Portal**. `main` declares what the
portal should contain; the publisher reconciles that declaration against what the portal
actually holds and makes the two match. You change the portal by changing this repository —
never by editing the portal directly (a hand edit is drift the next run will overwrite).

You do not need to read the Python to use it. You edit JSON and Markdown; four commands do
the rest.

---

## The mental model in one paragraph

Each product is a folder under `products/`. Its `manifest.json` declares the product and its
table of contents; Markdown files hold page bodies. `plan` shows the difference between the
repo and the live portal; `apply` makes the changes; `publish` promotes them from draft to
the live view consumers see. Re-running is always safe — the tool converges, so a repeated
run with nothing to do does nothing.

---

## Repository layout

```
products/
  Claims/                     ← folder name IS the product name (§A.4)
    manifest.json             ← the product's desired state
    getting-started.md        ← a Markdown page body
  Policy/
    manifest.json
    getting-started.md
```

One folder per product. The **folder name is the product's display name** — it is not in the
manifest, so the two can never disagree. Everything else lives in `manifest.json`.

---

## The four commands

Run these from the repo root (see the project README for environment setup).

| Command | What it does | Changes the portal? |
|---|---|---|
| `python -m publisher validate` | Loads and prints every manifest as understood | No |
| `python -m publisher plan` | Shows the diff between the repo and the live portal | No |
| `python -m publisher apply` | Creates and updates the portal to match the repo | **Yes (draft)** |
| `python -m publisher publish` | Promotes draft content to the live consumer view | **Yes (live)** |

`plan` and `publish --preview` are your safe pre-flight checks — **always read a `plan`
before an `apply`.** Exit codes: `0` nothing to do, `2` changes pending, `1` error.

> In steady state, CI runs `plan` on every pull request and `apply` on merge (see
> `CONTRIBUTING.md`, §A.13). The CI wrappers are still being built (§A.15 step 11); until
> then, a platform-team member runs `apply` and `publish` by hand after a PR merges.

---

## Creating a new product

1. **Branch.** Use the prefix for who is driving the change (`CONTRIBUTING.md`):
   `request/APR-####` for an approved intake request, `feat/<slug>` for platform work.
2. **Make the folder.** `products/<ProductName>/` — the folder name is the display name
   (3–40 characters).
3. **Write `manifest.json`.** Minimum: a `productMetadata` block (description, slug, owner)
   and a `contentMetadata` list of at least one entry. See the field reference below and copy
   an existing product (e.g. `products/Policy/`) as a template.
4. **Add page bodies.** For each `markdown`/`html` entry, add the file its `contentUrl`
   points at (a repo-relative path like `getting-started.md`).
5. **Validate.** `python -m publisher validate` — confirms the manifest parses and the tool
   understood it the way you meant.
6. **Preview.** `python -m publisher plan` — expect a block of `CREATE` lines for your new
   product. Nothing is written yet.
7. **Open a PR.** CI runs `plan`; a reviewer reads exactly what would change.
8. **Merge, apply, publish.** After merge: `apply` creates it (draft), then `publish` makes
   it visible.

---

## Updating an existing product

Same loop — **edit → `validate` → `plan` → PR → `apply` → `publish`** — but you are changing
files that already exist. What you edit determines what kind of update the tool plans.

### What counts as an update

An "update" is any change the reconciler can make **in place**, without replacing the
resource. There are four families:

| You want to… | Edit this | The plan shows |
|---|---|---|
| **Rename a product** | the folder name | `UPDATE product <slug> (name)` |
| Change a product's description | `productMetadata.description` | `UPDATE product <slug> (description)` |
| Make a product public / internal | `productMetadata.public` | `UPDATE product <slug> (public)` |
| Retire a product | `productMetadata.hidden: true` | `UPDATE product <slug> (hidden)` |
| **Rename a page or API entry** | the entry's `name` | `UPDATE toc-entry <slug>/<entry> (title)` |
| Reorder the table of contents | the entries' `order` values | `UPDATE toc-entry … (order)` |
| Re-point an API reference (new version) | the entry's `contentUrl` | `UPDATE toc-entry … (content_url)` |
| **Add a page or API entry** | add an item to `contentMetadata` | `CREATE toc-entry <slug>/<entry>` |
| Nest an entry under another | the entry's `parent` | (create/update with the new parent) |
| **Edit page text** | the Markdown/HTML file | `UPDATE document <slug>/<entry> (content)` |

So the four families are: **product metadata** (name, description, visibility, retirement),
**the table of contents** (rename, reorder, add, re-point entries), **page content** (the
file body), and **structure** (nesting via `parent`).

Order is compared as *relative sequence*, not absolute numbers — inserting one page in the
middle moves only what has to move, not the whole list. Renaming is always in place: a new
`name`/title never creates a duplicate.

### Adding and removing APIs (and pages) is updating the product too

An API reference is an `apiUrl` table-of-contents entry, so adding or removing an API *is* a
change to the product — its table of contents changes. Two things to know about how the tool
expresses them:

- **"Update" means two things.** The `UPDATE product (…)` plan line fires *only* for the
  product's own fields (name, description, public, hidden). Adding, removing or re-pointing an
  API changes the product's **table of contents** — a child resource — so it shows up as a
  `CREATE`/`DELETE`/`UPDATE` on a **`toc-entry`**, not on the `product` row. The product still
  changed; the change just landed on its contents.
- **Adding** an API works today → `CREATE toc-entry <product>/<api>`. **Re-pointing** an API to
  a new version is an in-place `UPDATE toc-entry … (content_url)`. **Removing** an API shows as
  an **orphan** (in the portal, absent from the repo) and is **not deleted yet** — actioned
  removal is step 9 (`--prune`). Until then a removed API lingers on the portal.

### What is NOT an in-place update — the two footguns

1. **Changing a `slug` is not a rename.** Slug is identity (§A.7). Change a product's or an
   entry's slug and the tool plans a **new resource plus an orphan** of the old one — it will
   look like a duplicate. To rename the *label*, change `name`, never `slug`. The `plan`
   makes this visible (a `CREATE` next to an `ORPHAN`), so read it.
2. **Changing an entry's `type`** (e.g. `markdown` → `apiUrl`) is **refused** in MVP1. Remove
   the old entry and add a new one instead.

### Things in the manifest that do NOT change the portal

- `owner` — generates CODEOWNERS for PR review (§A.10); it has no portal footprint.
- `logo` / `logoDark` — reserved; logo upload is not implemented yet.
- `autoPublish` — a hint for the publisher's own behaviour, not portal state.

---

## Removing things — not yet

Deleting a product or an entry is **not available yet** (it is the last, most careful step —
§A.15 step 9). Today, removing an entry or a whole product from the repo shows it as an
**orphan** in the `plan` (present in the portal, absent from the repo) but does not delete
it. To retire a product now, set `hidden: true` rather than deleting its folder. Deletion,
with its guardrails, comes later.

---

## Manifest field reference

```jsonc
{
  "productMetadata": {
    "description": "APIs for policy issuance and mid-term endorsements.", // required, ≤110 chars
    "slug": "policy",            // required, identity — 3–22 chars, [a-z0-9-_.]; DO NOT change to rename
    "public": false,             // optional, default false — visible beyond internal users
    "hidden": false,             // optional, default false — true retires the product
    "autoPublish": false,        // optional — publisher hint, no portal effect
    "owner": {                   // required — drives CODEOWNERS, not the portal
      "name": "Jane Doe",
      "email": "jane.doe@example.com",
      "githubHandle": "jdoe"
    }
  },
  "contentMetadata": [           // required, at least one entry
    {
      "order": 1,                // required — relative sequence (0-based ok)
      "name": "Getting Started", // required — the entry's title; change to rename in place
      "slug": "getting-started", // required — identity scoped to its parent; DO NOT change to rename
      "type": "markdown",        // required — markdown | html | apiUrl
      "contentUrl": "getting-started.md" // required — repo-relative path (md/html) OR absolute SwaggerHub URL (apiUrl)
      // "parent": "some-other-slug"     // optional — nest under another entry in this manifest
    }
  ]
}
```

The product's **display name comes from the folder**, not the manifest. API references use
the form `https://api.swaggerhub.com/apis/{org}/{api}/{version}/swagger.json`.

---

## A worked example — adding an API reference to Policy

1. `git checkout -b request/APR-0051`
2. Edit `products/Policy/manifest.json`, adding to `contentMetadata`:
   ```json
   {
     "order": 4,
     "name": "Policy Renewal API",
     "slug": "policy-renewal-api",
     "type": "apiUrl",
     "contentUrl": "https://api.swaggerhub.com/apis/sparklayerinc-55d/policy-renewal-api/1.0.0/swagger.json"
   }
   ```
3. `python -m publisher validate` → confirms Policy now has the new entry.
4. `python -m publisher plan` → `CREATE toc-entry policy/policy-renewal-api`.
5. Open a PR titled `APR-0051: add Policy Renewal API to Policy`, get it reviewed, merge.
6. `apply` creates the entry; `publish --preview` validates the API URL; `publish` makes it
   live.
