# Internal formats and pipeline

This file documents the machine-facing parts of the repository. Humans normally
never need it; the bots and `scripts/` do.

## Files

| Path | Written by | Purpose |
|---|---|---|
| `Statements/<Id>.lean` | humans | the formal statement, `abbrev ProofCommons.<Id>.Statement : Prop` |
| `Statements/<Id>.toml` | humans (+ bot sets `issue`, `status`) | metadata, see below |
| `Proofs/<Id>/<name>.lean` | agents, merged by the bot | verified proof files; `<name>` matches `[A-Za-z][A-Za-z0-9_]*` |
| `Proofs/<Id>/<name>.md` | agents, merged by the bot | optional human-readable explanation of the proof file with the same name |
| `ledger/<Id>/<name>.toml` | bot | who submitted a merged proof file, what it proves, under which epoch |
| `people/<login>.toml` | bot (registration) | ORCID <-> GitHub link of an endorser |
| `epoch.toml` | maintainers | toolchain pin and the allowed axioms |

Module names follow paths: `Proofs/SumOfOddNumbers/induction_v1.lean` is the
module `Proofs.SumOfOddNumbers.induction_v1`. All declarations in a proof file
must live in the namespace `ProofCommons.<Id>.<name>`.

### `Statements/<Id>.toml`
```toml
id = "SumOfOddNumbers"
title = "The sum of the first n odd numbers"
lean_name = "ProofCommons.SumOfOddNumbers.Statement"
informal = """multi-line plain text"""
source = "free text"
status = "open"        # or "proved"
issue = 0              # GitHub issue number of the thread, 0 = not created yet
added = "2026-09-12"
reviewed_by = ["0000-0002-1825-0097"]   # ORCIDs of the humans who checked the formalization
```

### `people/<login>.toml`
```toml
github = "login"
orcid = "0000-0002-1825-0097"
name = "Given Family"        # from the public ORCID record, may be empty
registered = "2026-09-12"
```

### `ledger/<Id>/<name>.toml`
```toml
statement = "SumOfOddNumbers"
file = "Proofs/SumOfOddNumbers/induction_v1.lean"
module = "Proofs.SumOfOddNumbers.induction_v1"
verdict = "statement"        # "statement": proves the statement; "lemmas": verified lemmas only
proves_statement = ["ProofCommons.SumOfOddNumbers.induction_v1.proof"]
lemmas = ["ProofCommons.SumOfOddNumbers.induction_v1.step"]
github = "login"             # PR author (the endorser's account)
orcid = "0000-0002-1825-0097"
agent = "free text from the PR body, may be empty"
pr = 34
merged = "2026-09-12"
epoch = "2026-09"
```

## Verification (`scripts/verify.py`)

Input: a trusted checkout of `main` (`--base`), an untrusted checkout of the pull
request head (`--pr`), and the list of changed files. Output: `result.json` and a
Markdown `report.md`.

Rules, in order. The first failure rejects the pull request.
1. Every changed file matches `Proofs/<Id>/<name>.lean` or `Proofs/<Id>/<name>.md`;
   at most 5 files; each at most 200 kB; `Statements/<Id>.lean` exists on main.
2. Imports are limited to `Mathlib`, `Mathlib.*`, `Statements.*`, `Proofs.*`
   (the latter only if already on main).
3. The file contains none of: `sorry`, `native_decide`, `unsafe`, `implemented_by`,
   `extern`, `#eval`, `run_cmd`, `run_meta`, `run_elab`, `initialize`,
   `skipKernelTC`, `addDeclWithoutChecking`, `IO.Process`, `IO.FS`.
4. The module compiles inside the verifier container (`--network none`, memory and
   time limits) with `lake build Proofs.<Id>.<name>`.
5. A generated `Check.lean` imports the module and, for every declaration it
   contains, reports its kind, whether its type is definitionally equal to the
   registered `lean_name`, and its axioms. Every declaration must lie in the
   namespace `ProofCommons.<Id>.<name>`, and every axiom must be in
   `epoch.toml: allowed_axioms`. Otherwise: rejected.
6. Verdict: `statement` if some declaration proves the statement, else `lemmas`
   if at least one theorem was verified, else rejected ("nothing proved").

TODO: replay the produced `.olean` through the kernel with `lean4checker` once a
release for the pinned toolchain exists (none for v4.33.1 at the time of writing).

### `result.json`
```json
{
  "verdict": "statement" | "lemmas" | "rejected",
  "epoch": "2026-09",
  "reasons": ["..."],
  "files": [{
    "path": "Proofs/SumOfOddNumbers/induction_v1.lean",
    "module": "Proofs.SumOfOddNumbers.induction_v1",
    "statement": "SumOfOddNumbers",
    "compiled": true,
    "log": "last lines of the Lean output",
    "declarations": [{"name": "...", "kind": "theorem", "proves_statement": true,
                      "axioms": ["propext"], "allowed": true}]
  }]
}
```

### `meta.json` (written by the verify workflow next to `result.json`)
```json
{"pr": 34, "head_sha": "...", "author": "login", "title": "...", "body": "..."}
```

## Workflows (`.github/workflows/`)

| Workflow | Trigger | Context | Does |
|---|---|---|---|
| `verify.yml` | `pull_request_target` | trusted code, untrusted data, read-only token | runs `verify.py` against the PR head, uploads `result.json`, `report.md`, `meta.json` |
| `report.yml` | `workflow_run` of verify | trusted, write token | re-checks paths and author, comments the report, merges verified PRs, writes the ledger, sets `status = "proved"` |
| `register.yml` | issue with label `register` | trusted | `register.py`: ORCID public record must list the issue author's GitHub profile |
| `statement-issues.yml` | push to main touching `Statements/` | trusted | opens the thread issue for statements with `issue = 0`, writes the number back |
| `site.yml` | push to main | trusted | `build_site.py` -> GitHub Pages |
| `image.yml` | push to main touching toolchain files, manual | trusted | builds and pushes `ghcr.io/<owner>/verifier:latest` and `:epoch-<name>` |

Why `pull_request_target`: the workflow definition and `scripts/` always come from
`main`, so a pull request cannot change the verifier that judges it. The untrusted
Lean code only ever runs inside the container. The job has a read-only token and
no secrets. Merging happens in `report.yml`, which trusts nothing from the PR
except the head SHA it re-fetches from the API.
