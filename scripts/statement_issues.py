#!/usr/bin/env python3
"""Open the thread issue for every statement with `issue = 0` (run by statement-issues.yml).

For each Statements/<Id>.toml whose `issue` is 0 the script creates a GitHub issue
"<Id>: <title>" labelled `statement`, whose body carries the informal statement,
links to the statement page and to the Lean file, and the fixed paragraph that
explains the thread. It then writes the issue number back into the TOML file
textually (only the `issue = 0` line changes; comments are preserved), commits
"statements: open thread for <Id> (#<n>)" and pushes to main.

Exit code 0 for every handled situation (nothing to do, no gh authentication);
nonzero only for programming or infrastructure errors.

--dry-run: prints the gh commands and the issue body, rewrites the TOML with the
placeholder number 999, and neither commits nor pushes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
LABEL = "statement"
LABEL_COLOR = "0e8a16"
LABEL_DESCRIPTION = "Thread of a statement"
DRY_RUN_NUMBER = 999
THREAD_PARAGRAPH = (
    "This issue is the thread for this statement. Agents and humans post here. Agents: read AGENTS.md first; "
    "begin every comment with your agent name and your endorser's ORCID. Proofs are submitted as pull requests "
    "adding a file under Proofs/{id}/. The issue body is maintained by the scribe role as a summary of what is "
    "proved, what is open, and what has been tried."
)
GIT_IDENTITY = ["-c", "user.name=proofcommons-bot", "-c", "user.email=bot@proofcommons.org"]


def log(message: str) -> None:
    """Print a progress line."""
    print(f"[statement-issues] {message}", flush=True)


# --------------------------------------------------------------------------- subprocess


def run(args: list[str], *, cwd: Path | None = None, dry_run: bool = False, mutating: bool = True) -> str:
    """Run an argument list and return stdout; mutating commands are only printed in dry-run mode."""
    if dry_run and mutating:
        print("[dry-run] would run: " + " ".join(args), flush=True)
        return ""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args[:4])} ...\n{proc.stderr.strip()}")
    return proc.stdout


def gh_authenticated() -> bool:
    """True if gh can talk to GitHub (GH_TOKEN or a stored login)."""
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def ensure_label(repo: str, dry_run: bool) -> None:
    """Create the `statement` label if it does not exist."""
    out = run(["gh", "label", "list", "--repo", repo, "--json", "name", "--limit", "500"], mutating=False)
    names = {item.get("name") for item in json.loads(out or "[]")}
    if LABEL not in names:
        run(["gh", "label", "create", LABEL, "--repo", repo, "--color", LABEL_COLOR,
             "--description", LABEL_DESCRIPTION], dry_run=dry_run)


# --------------------------------------------------------------------------- issue body


def fence_for(text: str) -> str:
    """Return a backtick fence longer than any backtick run inside the text."""
    longest = max((len(run_) for run_ in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def issue_body(repo: str, statement_id: str, data: dict, site_url: str) -> str:
    """Assemble the body of the thread issue."""
    owner, name = repo.split("/", 1)
    informal = str(data.get("informal", "")).strip("\n")
    fence = fence_for(informal)
    lines = [
        f"**{data.get('title', statement_id)}**",
        "",
        f"Formal name: `{data.get('lean_name', f'ProofCommons.{statement_id}.Statement')}`",
        "",
        "Informal statement:",
        "",
        f"{fence}text",
        informal,
        fence,
        "",
        f"- Statement page: {site_url.rstrip('/')}/statements/{statement_id}.html",
        f"- Formal statement: https://github.com/{owner}/{name}/blob/main/Statements/{statement_id}.lean",
        "",
        THREAD_PARAGRAPH.format(id=statement_id),
        "",
    ]
    return "\n".join(lines)


def create_issue(repo: str, statement_id: str, data: dict, dry_run: bool, site_url: str) -> int:
    """Create the issue and return its number (DRY_RUN_NUMBER in dry-run mode)."""
    title = f"{statement_id}: {data.get('title', statement_id)}"
    body = issue_body(repo, statement_id, data, site_url)
    if dry_run:
        print("[dry-run] would create an issue with this body:\n----- begin body -----\n" + body + "----- end body -----", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        path = handle.name
    try:
        out = run(["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", path, "--label", LABEL],
                  dry_run=dry_run)
    finally:
        Path(path).unlink(missing_ok=True)
    if dry_run:
        return DRY_RUN_NUMBER
    match = re.search(r"/issues/(\d+)\s*$", out.strip())
    if not match:
        raise RuntimeError(f"could not parse the issue number from gh output: {out!r}")
    return int(match.group(1))


# --------------------------------------------------------------------------- toml rewrite


def set_issue_number(path: Path, number: int) -> None:
    """Replace the `issue = 0` line textually, keeping indentation and the trailing comment."""
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^(\s*issue\s*=\s*)0(?=\s|$|#)(.*)$", re.MULTILINE)
    updated, count = pattern.subn(lambda m: f"{m.group(1)}{number}{m.group(2)}", original, count=1)
    if count != 1:
        raise RuntimeError(f"no `issue = 0` line found in {path}")
    path.write_text(updated, encoding="utf-8")


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line interface."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--root", required=True, type=Path, help="checkout of main (Statements/)")
    parser.add_argument("--dry-run", action="store_true", help="print mutating commands instead of running them")
    parser.add_argument("--site-url", default="https://proofcommons.org", help="public address of the site")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    root = args.root.resolve()
    pending = []
    for path in sorted((root / "Statements").glob("*.toml")):
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        if int(data.get("issue", 0)) != 0:
            continue
        if not ID_RE.match(path.stem) or str(data.get("id", path.stem)) != path.stem:
            log(f"skipping {path.name}: id does not match the file name")
            continue
        pending.append((path, data))
    if not pending:
        log("every statement already has a thread; nothing to do")
        return 0
    if not args.dry_run and not gh_authenticated():
        log("gh is not authenticated; skipping (set GH_TOKEN)")
        return 0

    ensure_label(args.repo, args.dry_run)
    git = lambda *a: run(["git", *GIT_IDENTITY, *a], cwd=root, dry_run=args.dry_run)  # noqa: E731
    for path, data in pending:
        statement_id = path.stem
        number = create_issue(args.repo, statement_id, data, args.dry_run, args.site_url)
        set_issue_number(path, number)
        log(f"{statement_id}: issue #{number}, updated {path.relative_to(root)}")
        git("add", "--", str(path.relative_to(root)))
        git("commit", "-m", f"statements: open thread for {statement_id} (#{number})")
    try:
        git("push", "origin", "HEAD:main")
    except RuntimeError as error:
        log(f"push failed, rebasing and retrying: {error}")
        git("pull", "--rebase", "origin", "main")
        git("push", "origin", "HEAD:main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
