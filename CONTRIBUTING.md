# Contributing

## Conventional commits

Use [Conventional Commits](https://www.conventionalcommits.org/) on `main` (usually via the PR title / squash message):

| Prefix | Semver |
|--------|--------|
| `fix:` / `chore:` / `docs:` / `refactor:` / `perf:` / `deps:` | patch |
| `feat:` | minor |
| `feat!:` / `fix!:` / `BREAKING CHANGE:` footer | major |

Do **not** bump `pyproject.toml` or `__version__` in feature PRs. Merges to `main` open or update a **release-please** PR that bumps versions and `CHANGELOG.md` together.

## Releases

1. Merge feature/fix PRs to `main` with conventional commit messages.
2. Review and merge the `chore(main): release …` PR from release-please.
3. Merging that PR tags the release (`vX.Y.Z`) and publishes the GitHub Release.
