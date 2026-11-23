---
name: conventional-pr-titles
description: >-
  Use conventional-commit PR titles (fix:/feat:/chore:/docs:/ci:) for
  lithium-bom and lithium-systemgroup so release-please can bump versions.
  Use when opening or updating a PR in either repo.
---

# Conventional PR titles (release-please)

Applies to **lithium-bom** and **lithium-systemgroup**. Both use release-please on `main`. Squash-merge uses the **PR title** as the commit subject, so the title must be a conventional commit.

## Required title shape

```
<type>: <short description>
```

Common types:

| Type | When | Version bump |
| --- | --- | --- |
| `fix:` | Bug fix, typo, wrong behavior | patch |
| `feat:` | User-visible feature | minor |
| `chore:` | Tooling, deps, scripts with no user-facing change | none (unless configured) |
| `docs:` | Docs only | none |
| `ci:` | CI/workflow only | none |

Breaking change: add `!` after the type (`feat!: …`) or a `BREAKING CHANGE:` footer.

## Rules

1. Set the conventional title **when creating the PR** (ManagePullRequest / `gh pr create`), not only in the branch commit.
2. Prefer `fix:` for most bugfixes and UI polish; use `feat:` only for new capability.
3. Do not use bare titles like `Format Order quantity (kg) with thousands separators` — prefix with `fix:` or `feat:`.
4. Scope is optional: `fix(materials): …` is fine.

## Examples

- `fix: format order quantity with thousands separators`
- `feat: two-line material list primary cell`
- `chore: add one-time set_px_products_to_powder command`
- `ci: wire release-please for semver on main`
