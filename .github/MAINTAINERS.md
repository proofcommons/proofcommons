# Notes for maintainers

**What the bots assume about the repository.** If one of these is changed, the
corresponding workflow stops working.

- GitHub Pages is published from GitHub Actions (the `site` workflow), with the
  custom domain proofcommons.org configured in the Pages settings.
- The container image `ghcr.io/proofcommons/verifier` is public. The `verify`
  workflow pulls it without credentials, and so do endorsers.
- The ruleset on `main` requires a pull request with a review from code owners,
  and lets the GitHub Actions app bypass it. Together with `CODEOWNERS` this
  means: changes to `Statements/`, `scripts/`, `.github/` and the toolchain
  files need a maintainer's approval, while pull requests touching only
  `Proofs/` are merged by the `report` workflow, which also pushes ledger
  commits to `main`.
- The `verify` status check is not required by the ruleset. It runs under
  `pull_request_target` and reports through `report`, which merges only what
  it verified itself.
- Every workflow declares its own `permissions`, so the repository default for
  the workflow token can stay read-only.

**Files written by bots.** `ledger/` and `people/` are written by the `report`
and `register` workflows and should not be edited by hand, except to correct a
mistake. `Statements/<Id>.toml` has two bot-maintained fields: `issue`, set
once by `statement-issues`, and `status`, set to `"proved"` by `report`.

**Labels** are created on demand: `verified`, `rejected`, `statement`,
`register`.

**Adding a statement.** A pull request adding `Statements/<Id>.lean` with
`abbrev ProofCommons.<Id>.Statement : Prop` and `Statements/<Id>.toml` with
`issue = 0`. The reviewers who checked that the Lean statement matches the
informal one add their ORCID iDs to `reviewed_by`. After the merge, the
`statement-issues` workflow opens the thread and writes its number into the
file; the `image` workflow rebuilds the verifier image with the new statement.

**Rotating the epoch.** Change `lean-toolchain`, `lakefile.toml`,
`lake-manifest.json` and `epoch.toml` in one pull request, rebuild the image,
and re-run `scripts/verify.py` over every file under `Proofs/` before merging.
Proofs that no longer compile are kept, with their ledger entries marked, and
their statements get a thread comment asking for a repair.
