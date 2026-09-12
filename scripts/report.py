#!/usr/bin/env python3
"""Trusted side of the verification pipeline (run by .github/workflows/report.yml).

Reads the artifact uploaded by verify.yml (meta.json, result.json, report.md),
re-fetches the pull request from the GitHub API and re-checks everything that
matters for trust: the PR is still open, the head SHA is the one that was
verified, every changed path lies under Proofs/ with the allowed pattern, and the
author is a registered endorser (people/<login>.toml). It then posts one comment,
sets the verified/rejected label and, for verified pull requests of registered
endorsers, squash-merges the PR, writes ledger/<Id>/<name>.toml, marks proved
statements in Statements/<Id>.toml and pushes to main.

Everything that comes from the pull request (title, body, file names, report
text) is untrusted: it never goes through a shell (all subprocess calls use
argument lists) and never decides which files are written beyond the validated
path pattern PROOF_PATH_RE.

Exit code 0 for every handled situation (stale result, closed PR, unregistered
author, rejected verdict, ...); nonzero only for programming or infrastructure
errors (API failures, merge or push failures).

--dry-run: read-only gh calls are still executed (put a stub `gh` on PATH for
tests), every mutating gh or git command is printed instead of run, and the
ledger / statement files are written locally without commit or push.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROOF_PATH_RE = re.compile(r"^Proofs/[A-Za-z0-9_]+/[A-Za-z][A-Za-z0-9_]*\.(lean|md)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
MARKER_TEMPLATE = "<!-- proofcommons-verifier sha={sha} -->"
LABELS = {"verified": "2a7a2a", "rejected": "b60205"}
LABEL_DESCRIPTIONS = {
    "verified": "The verifier accepted this pull request",
    "rejected": "The verifier rejected this pull request",
}
VERDICTS = ("statement", "lemmas", "rejected")
MAX_COMMENT_REPORT_CHARS = 60000
MAX_AGENT_CHARS = 200
GIT_IDENTITY = ["-c", "user.name=proofcommons-bot", "-c", "user.email=bot@proofcommons.org"]


def log(message: str) -> None:
    """Print a progress line (flushed so that it interleaves with subprocess output)."""
    print(f"[report] {message}", flush=True)


# --------------------------------------------------------------------------- subprocess


def run(args: list[str], *, cwd: Path | None = None, dry_run: bool = False, mutating: bool = True) -> str:
    """Run a command given as an argument list and return its stdout.

    Mutating commands are only printed in dry-run mode; read-only commands
    (mutating=False) are always executed. Raises on nonzero exit.
    """
    if dry_run and mutating:
        print("[dry-run] would run: " + " ".join(args), flush=True)
        return ""
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args[:4])} ...\n{proc.stderr.strip()}")
    return proc.stdout


def gh_json(args: list[str]) -> object:
    """Run a read-only gh command and parse its JSON output."""
    out = run(["gh", *args], mutating=False)
    return json.loads(out)


# --------------------------------------------------------------------------- inputs


def load_artifact(artifact: Path, root: Path) -> tuple[dict, dict, str] | None:
    """Return (meta, result, report_md) from the artifact directory, or None if unusable."""
    meta_path = artifact / "meta.json"
    if not meta_path.is_file():
        log(f"no {meta_path}; nothing to do")
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    result_path = artifact / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        log("no result.json in the artifact; recording an infrastructure failure")
        result = {
            "verdict": "rejected",
            "epoch": read_epoch(root),
            "reasons": ["verifier infrastructure failure, see the workflow log"],
            "files": [],
        }
    report_path = artifact / "report.md"
    report_md = report_path.read_text(encoding="utf-8") if report_path.is_file() else "The verifier produced no report."
    return meta, result, report_md


def read_epoch(root: Path) -> str:
    """Return the epoch name from epoch.toml."""
    with (root / "epoch.toml").open("rb") as handle:
        return str(tomllib.load(handle)["name"])


def fetch_pr(repo: str, number: int) -> dict:
    """Re-fetch the pull request from the API (the only trusted view of it)."""
    fields = "number,headRefOid,author,files,state,isCrossRepository,body,title"
    data = gh_json(["pr", "view", str(number), "--repo", repo, "--json", fields])
    if not isinstance(data, dict):
        raise RuntimeError("unexpected gh pr view output")
    return data


def find_person(root: Path, login: str) -> dict | None:
    """Return the parsed people/<login>.toml (case-insensitive on the stem), or None."""
    people = root / "people"
    if not people.is_dir():
        return None
    for path in sorted(people.glob("*.toml")):
        if path.stem.lower() == login.lower():
            with path.open("rb") as handle:
                return tomllib.load(handle)
    return None


def agent_from_body(body: str) -> str:
    """Return the value of the first `Agent: ...` line of the PR body (may be empty)."""
    for line in body.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^\s*Agent:\s*(.*?)\s*$", line)
        if match:
            return match.group(1)[:MAX_AGENT_CHARS]
    return ""


# --------------------------------------------------------------------------- comment and labels


def display(text: str) -> str:
    """Make an untrusted string safe to show inline in Markdown (no newlines, no backticks)."""
    return re.sub(r"[`\r\n]", "", text)


def build_comment(*, sha: str, epoch: str, verdict: str, report_md: str, extra_reasons: list[str],
                  registered: bool, author: str, repo: str, merged: bool, merge_error: str | None) -> str:
    """Assemble the single bot comment for the pull request."""
    parts = [MARKER_TEMPLATE.format(sha=sha), f"**Verifier · epoch {epoch} · verdict: {verdict}**", ""]
    report = report_md.strip()
    if len(report) > MAX_COMMENT_REPORT_CHARS:
        report = report[:MAX_COMMENT_REPORT_CHARS] + "\n\n... (report truncated; the full report is in the verify-result artifact)"
    parts.append(report)
    if extra_reasons:
        parts.append("")
        parts.append("Rejected by the report step:")
        parts.extend(f"- {reason}" for reason in extra_reasons)
    parts.append("")
    parts.append("---")
    if verdict in ("statement", "lemmas"):
        if not registered:
            parts.append(
                f"The proof was verified, but it is not merged: the pull request author `{display(author)}` is not a "
                f"registered endorser (there is no `people/{display(author)}.toml`). To register, open a registration "
                f"issue: https://github.com/{repo}/issues/new?template=register.yml -- the bot checks that your public "
                f"ORCID record lists https://github.com/{display(author)}. Once registered, close and reopen this pull "
                "request (or push a new commit) to run the verifier again; it is then merged automatically."
            )
        elif merged:
            parts.append("Merged.")
        else:
            parts.append(
                "The proof was verified, but the automatic merge failed; a maintainer has to look at the workflow log."
                + (f" Error: `{display(merge_error)}`" if merge_error else "")
            )
    else:
        parts.append("Not merged. Fix the issues above and push a new commit to run the verifier again.")
    return "\n".join(parts) + "\n"


def already_commented(repo: str, number: int, sha: str) -> bool:
    """True if a bot comment with the marker for this head SHA already exists on the PR."""
    marker = MARKER_TEMPLATE.format(sha=sha)
    data = gh_json(["api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp"])
    comments: list = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list):
                comments.extend(item)
            elif isinstance(item, dict):
                comments.append(item)
    return any(marker in str(comment.get("body") or "") for comment in comments)


def post_comment(repo: str, number: int, body: str, dry_run: bool) -> None:
    """Post the comment via `gh pr comment --body-file` (the body never touches a shell)."""
    if dry_run:
        print("[dry-run] would post this comment:\n----- begin comment -----\n" + body + "----- end comment -----", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        path = handle.name
    try:
        run(["gh", "pr", "comment", str(number), "--repo", repo, "--body-file", path], dry_run=dry_run)
    finally:
        Path(path).unlink(missing_ok=True)


def ensure_labels(repo: str, dry_run: bool) -> None:
    """Create the verified/rejected labels if they do not exist yet."""
    existing = gh_json(["label", "list", "--repo", repo, "--json", "name", "--limit", "500"])
    names = {item.get("name") for item in existing} if isinstance(existing, list) else set()
    for name, color in LABELS.items():
        if name not in names:
            run(["gh", "label", "create", name, "--repo", repo, "--color", color,
                 "--description", LABEL_DESCRIPTIONS[name]], dry_run=dry_run)


def set_label(repo: str, number: int, verdict: str, dry_run: bool) -> None:
    """Add the label matching the verdict and remove the other one."""
    add = "verified" if verdict in ("statement", "lemmas") else "rejected"
    remove = "rejected" if add == "verified" else "verified"
    run(["gh", "pr", "edit", str(number), "--repo", repo, "--add-label", add, "--remove-label", remove], dry_run=dry_run)


# --------------------------------------------------------------------------- merge and ledger


def merge_pr(repo: str, number: int, cross_repository: bool, dry_run: bool) -> None:
    """Squash-merge the pull request; the branch is deleted only for same-repository PRs."""
    args = ["gh", "pr", "merge", str(number), "--repo", repo, "--squash"]
    if not cross_repository:
        args.append("--delete-branch")
    run(args, dry_run=dry_run)


def toml_string(value: str) -> str:
    """Quote a Python string as a TOML basic string."""
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def toml_string_list(values: list[str]) -> str:
    """Quote a list of strings as a TOML array."""
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def ledger_entries(result: dict, pr_files: set[str]) -> list[dict]:
    """Return one entry per verified .lean file: only paths that match the pattern and are in the PR."""
    entries = []
    for item in result.get("files") or []:
        path = str(item.get("path", ""))
        if not PROOF_PATH_RE.match(path) or not path.endswith(".lean") or path not in pr_files:
            continue
        _, statement_id, filename = path.split("/")
        name = filename[: -len(".lean")]
        proves, lemmas = [], []
        for decl in item.get("declarations") or []:
            decl_name = str(decl.get("name", ""))
            if decl.get("proves_statement"):
                proves.append(decl_name)
            elif str(decl.get("kind", "")) in ("theorem", "lemma"):
                lemmas.append(decl_name)
        entries.append({"path": path, "statement": statement_id, "name": name,
                        "module": f"Proofs.{statement_id}.{name}", "proves": proves, "lemmas": lemmas})
    return entries


def write_ledger(root: Path, entry: dict, *, author: str, orcid: str, agent: str, pr: int, epoch: str, today: str) -> Path:
    """Write ledger/<Id>/<name>.toml in the format of scripts/README.md and return its path."""
    path = root / "ledger" / entry["statement"] / f"{entry['name']}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    verdict = "statement" if entry["proves"] else "lemmas"
    text = "\n".join([
        f"statement = {toml_string(entry['statement'])}",
        f"file = {toml_string(entry['path'])}",
        f"module = {toml_string(entry['module'])}",
        f"verdict = {toml_string(verdict)}",
        f"proves_statement = {toml_string_list(entry['proves'])}",
        f"lemmas = {toml_string_list(entry['lemmas'])}",
        f"github = {toml_string(author)}",
        f"orcid = {toml_string(orcid)}",
        f"agent = {toml_string(agent)}",
        f"pr = {int(pr)}",
        f"merged = {toml_string(today)}",
        f"epoch = {toml_string(epoch)}",
    ]) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def mark_proved(root: Path, statement_id: str) -> Path | None:
    """Set status = "proved" in Statements/<Id>.toml textually; return the path if it changed."""
    path = root / "Statements" / f"{statement_id}.toml"
    if not path.is_file():
        log(f"warning: {path} does not exist; cannot set status")
        return None
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(r'^(\s*status\s*=\s*)"[^"\n]*"(.*)$', re.MULTILINE)
    updated, count = pattern.subn(lambda m: f'{m.group(1)}"proved"{m.group(2)}', original, count=1)
    if count == 0:
        log(f"warning: no status line in {path}")
        return None
    if updated == original:
        return None
    path.write_text(updated, encoding="utf-8")
    return path


def git(root: Path, args: list[str], dry_run: bool, mutating: bool = True) -> str:
    """Run git in the repository root."""
    return run(["git", *GIT_IDENTITY, *args], cwd=root, dry_run=dry_run, mutating=mutating)


def push_with_retry(root: Path, dry_run: bool, attempts: int = 3) -> None:
    """Push main; on rejection rebase onto the remote and try again."""
    for attempt in range(1, attempts + 1):
        try:
            git(root, ["push", "origin", "HEAD:main"], dry_run)
            return
        except RuntimeError as error:
            log(f"push attempt {attempt} failed: {error}")
            if attempt == attempts:
                raise
            git(root, ["pull", "--rebase", "origin", "main"], dry_run)


def record_ledger(root: Path, *, entries: list[dict], verdict: str, author: str, orcid: str, agent: str,
                  pr: int, dry_run: bool) -> None:
    """After the merge: pull main, write ledger files, mark proved statements, commit and push."""
    git(root, ["pull", "--ff-only", "origin", "main"], dry_run)
    epoch = read_epoch(root)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    changed: list[Path] = []
    for entry in entries:
        changed.append(write_ledger(root, entry, author=author, orcid=orcid, agent=agent, pr=pr, epoch=epoch, today=today))
        log(f"wrote {changed[-1].relative_to(root)}")
    if verdict == "statement":
        proved_ids = sorted({e["statement"] for e in entries if e["proves"]}) or sorted({e["statement"] for e in entries})
        for statement_id in proved_ids:
            path = mark_proved(root, statement_id)
            if path is not None:
                changed.append(path)
                log(f"set status = \"proved\" in {path.relative_to(root)}")
    if not changed:
        log("nothing to record")
        return
    names = ", ".join(f"{e['statement']}/{e['name']}" for e in entries)
    message = f"ledger: {names} (#{pr})"
    git(root, ["add", "--", *[str(p.relative_to(root)) for p in changed]], dry_run)
    if not dry_run:
        staged = git(root, ["diff", "--cached", "--name-only"], dry_run, mutating=False).strip()
        if not staged:
            log("no staged changes; skipping commit")
            return
    git(root, ["commit", "-m", message], dry_run)
    push_with_retry(root, dry_run)


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line interface."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--artifact", required=True, type=Path, help="directory with meta.json, result.json, report.md")
    parser.add_argument("--root", required=True, type=Path, help="checkout of main (people/, ledger/, Statements/, epoch.toml)")
    parser.add_argument("--dry-run", action="store_true", help="print mutating commands instead of running them")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    root = args.root.resolve()
    loaded = load_artifact(args.artifact, root)
    if loaded is None:
        return 0
    meta, result, report_md = loaded

    number = int(meta["pr"])
    head_sha = str(meta.get("head_sha", ""))
    if not SHA_RE.match(head_sha):
        log(f"meta.json has no valid head_sha ({head_sha!r}); nothing to do")
        return 0

    pr = fetch_pr(args.repo, number)
    if pr.get("state") != "OPEN":
        log(f"PR #{number} is {pr.get('state')}; nothing to do")
        return 0
    if pr.get("headRefOid") != head_sha:
        log(f"stale result: verified {head_sha}, PR head is now {pr.get('headRefOid')}")
        return 0

    author = str((pr.get("author") or {}).get("login") or "")
    pr_files = [str(f.get("path", "")) for f in pr.get("files") or []]
    verdict = str(result.get("verdict", "rejected"))
    extra_reasons: list[str] = []
    outside = [p for p in pr_files if not PROOF_PATH_RE.match(p)]
    if outside:
        verdict = "rejected"
        extra_reasons.append("files outside Proofs/: " + ", ".join(f"`{display(p)}`" for p in outside))
    if not pr_files:
        verdict = "rejected"
        extra_reasons.append("the pull request changes no files")
    if verdict not in VERDICTS:
        extra_reasons.append(f"unknown verdict `{display(verdict)}`")
        verdict = "rejected"
    epoch = str(result.get("epoch") or read_epoch(root))

    person = find_person(root, author) if LOGIN_RE.match(author) else None
    registered = person is not None
    log(f"PR #{number} by {author!r}: verdict={verdict} registered={registered} files={len(pr_files)}")

    merged, merge_error = False, None
    should_merge = verdict in ("statement", "lemmas") and registered
    if should_merge:
        try:
            merge_pr(args.repo, number, bool(pr.get("isCrossRepository")), args.dry_run)
            merged = True
        except RuntimeError as error:
            merge_error = str(error).splitlines()[-1] if str(error) else "unknown"
            log(f"merge failed: {error}")

    if already_commented(args.repo, number, head_sha):
        log(f"a bot comment for {head_sha} already exists; not commenting again")
    else:
        body = build_comment(sha=head_sha, epoch=epoch, verdict=verdict, report_md=report_md,
                             extra_reasons=extra_reasons, registered=registered, author=author,
                             repo=args.repo, merged=merged, merge_error=merge_error)
        post_comment(args.repo, number, body, args.dry_run)

    ensure_labels(args.repo, args.dry_run)
    set_label(args.repo, number, verdict, args.dry_run)

    if should_merge and not merged:
        return 1
    if merged:
        entries = ledger_entries(result, set(pr_files))
        record_ledger(root, entries=entries, verdict=verdict, author=author, orcid=str(person.get("orcid", "")),
                      agent=agent_from_body(str(pr.get("body") or "")), pr=number, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
