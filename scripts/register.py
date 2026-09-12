#!/usr/bin/env python3
"""Register an endorser: link an ORCID iD to a GitHub login (run by register.yml).

The issue (label `register`, opened from the issue form) contains an ORCID iD. The
registration succeeds iff the public ORCID record of that iD lists the issue
author's GitHub profile, https://github.com/<login>, under "Websites & social
links" (the `researcher-urls` of the public API). On success people/<login>.toml
is written, committed and pushed, and the issue is closed with a confirmation.
On failure the bot explains exactly what it expected and what it found, and the
issue stays open; editing the issue triggers a re-check.

Nothing from the issue is executed: the body is only searched for the ORCID
pattern, and every subprocess call uses an argument list.

Exit code 0 for every handled situation (no ORCID, bad checksum, link missing,
ORCID unreachable, already registered, conflict); nonzero only for programming or
infrastructure errors (GitHub API failure, push failure).

--dry-run: `gh issue view` is still executed (put a stub `gh` on PATH for tests);
mutating gh and git commands are printed, and people/<login>.toml is written
locally without commit or push. --orcid-api lets tests point at a stub server.
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
import urllib.error
import urllib.request
from pathlib import Path

ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
DEFAULT_ORCID_API = "https://pub.orcid.org/v3.0"
USER_AGENT = "proofcommons-bot"
GIT_IDENTITY = ["-c", "user.name=proofcommons-bot", "-c", "user.email=bot@proofcommons.org"]


def log(message: str) -> None:
    """Print a progress line."""
    print(f"[register] {message}", flush=True)


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


def fetch_issue(repo: str, number: int) -> dict:
    """Fetch author, body, title and state of the issue."""
    out = run(["gh", "issue", "view", str(number), "--repo", repo, "--json", "author,body,title,state"], mutating=False)
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected gh issue view output")
    return data


def comment(repo: str, number: int, text: str, dry_run: bool) -> None:
    """Comment on the issue through --body-file (the text never touches a shell)."""
    if dry_run:
        print("[dry-run] would post this comment:\n----- begin comment -----\n" + text + "\n----- end comment -----", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        path = handle.name
    try:
        run(["gh", "issue", "comment", str(number), "--repo", repo, "--body-file", path], dry_run=dry_run)
    finally:
        Path(path).unlink(missing_ok=True)


def ensure_label(repo: str, number: int, dry_run: bool) -> None:
    """Create the `register` label if missing and put it on the issue.

    Issue forms only apply labels that already exist, so the first registration
    ever would otherwise stay unlabelled. Failures here are logged, not fatal.
    """
    try:
        run(["gh", "label", "create", "register", "--repo", repo, "--color", "1d76db",
             "--description", "ORCID registration request", "--force"], dry_run=dry_run)
        run(["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", "register"], dry_run=dry_run)
    except RuntimeError as exc:
        log(f"could not apply the register label: {exc}")


def close_issue(repo: str, number: int, dry_run: bool) -> None:
    """Close the issue as completed."""
    run(["gh", "issue", "close", str(number), "--repo", repo, "--reason", "completed"], dry_run=dry_run)


# --------------------------------------------------------------------------- ORCID


def orcid_checksum_ok(orcid: str) -> bool:
    """Validate the ORCID check digit (ISO 7064 MOD 11-2 over the 15 base digits)."""
    digits = orcid.replace("-", "")
    if len(digits) != 16:
        return False
    total = 0
    for char in digits[:-1]:
        if not char.isdigit():
            return False
        total = (total + int(char)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    expected = "X" if result == 10 else str(result)
    return digits[-1] == expected


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    """GET a JSON document from the ORCID public API."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def researcher_urls(api: str, orcid: str) -> list[str]:
    """Return every URL listed under 'Websites & social links' of the public record."""
    data = fetch_json(f"{api}/{orcid}/researcher-urls")
    urls = []
    for item in data.get("researcher-url") or []:
        value = ((item or {}).get("url") or {}).get("value")
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())
    return urls


def person_name(api: str, orcid: str) -> str:
    """Return 'Given Family' from the public record; empty if absent or unreachable."""
    try:
        data = fetch_json(f"{api}/{orcid}/person")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        log(f"could not fetch the name: {error}")
        return ""
    name = data.get("name") or {}
    given = ((name.get("given-names") or {}).get("value") or "").strip()
    family = ((name.get("family-name") or {}).get("value") or "").strip()
    return " ".join(part for part in (given, family) if part)


def normalize_url(url: str) -> str:
    """lowercase, strip scheme, strip www., strip trailing slashes."""
    value = url.strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def github_link_present(urls: list[str], login: str) -> bool:
    """True if one of the URLs is the GitHub profile of the login."""
    target = f"github.com/{login.lower()}"
    return any(normalize_url(url) == target for url in urls)


# --------------------------------------------------------------------------- people file


def find_person(root: Path, login: str) -> tuple[Path, dict] | None:
    """Return (path, data) of an existing people file for the login (case-insensitive stem)."""
    people = root / "people"
    if not people.is_dir():
        return None
    for path in sorted(people.glob("*.toml")):
        if path.stem.lower() == login.lower():
            with path.open("rb") as handle:
                return path, tomllib.load(handle)
    return None


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


def write_person(root: Path, login: str, orcid: str, name: str) -> Path:
    """Write people/<login>.toml in the format of scripts/README.md."""
    path = root / "people" / f"{login}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    text = "\n".join([
        f"github = {toml_string(login)}",
        f"orcid = {toml_string(orcid)}",
        f"name = {toml_string(name)}",
        f"registered = {toml_string(today)}",
    ]) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def commit_and_push(root: Path, path: Path, login: str, dry_run: bool) -> None:
    """Commit the people file and push to main (with one rebase retry)."""
    git = lambda *a, **k: run(["git", *GIT_IDENTITY, *a], cwd=root, dry_run=dry_run, **k)  # noqa: E731
    git("add", "--", str(path.relative_to(root)))
    git("commit", "-m", f"register: {login}")
    try:
        git("push", "origin", "HEAD:main")
    except RuntimeError as error:
        log(f"push failed, rebasing and retrying: {error}")
        git("pull", "--rebase", "origin", "main")
        git("push", "origin", "HEAD:main")


# --------------------------------------------------------------------------- messages


def display(text: str) -> str:
    """Make an untrusted string safe to show inline in Markdown (no newlines, no backticks)."""
    return re.sub(r"[`\r\n]", "", text)


def failure_message(login: str, orcid: str, urls: list[str]) -> str:
    """Explain precisely what was expected and what was found."""
    lines = [
        "Thanks for registering. I could not confirm the link between this GitHub account and the ORCID iD yet.",
        "",
        f"I read the public ORCID record https://orcid.org/{orcid} and expected to find the address of your GitHub "
        f"profile, `https://github.com/{login}`, in its \"Websites & social links\" section.",
    ]
    if urls:
        lines.append("The URLs I found there are:")
        lines.extend(f"- {display(url)}" for url in urls)
    else:
        lines.append("That section is empty or not public, so I found no URLs at all.")
    lines += [
        "",
        f"To fix this: open https://orcid.org/my-orcid, add `https://github.com/{login}` under \"Websites & social "
        "links\", set its visibility to \"Everyone\", and save. Then edit this issue (any edit, for example re-saving "
        "the form) to trigger a re-check.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line interface."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--issue", required=True, type=int, help="issue number of the registration request")
    parser.add_argument("--root", required=True, type=Path, help="checkout of main (people/)")
    parser.add_argument("--dry-run", action="store_true", help="print mutating commands instead of running them")
    parser.add_argument("--orcid-api", default=DEFAULT_ORCID_API, help="base URL of the ORCID public API (for tests)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    root = args.root.resolve()
    issue = fetch_issue(args.repo, args.issue)
    if issue.get("state", "OPEN") != "OPEN":
        log(f"issue #{args.issue} is {issue.get('state')}; nothing to do")
        return 0
    ensure_label(args.repo, args.issue, args.dry_run)
    login = str((issue.get("author") or {}).get("login") or "")
    if not LOGIN_RE.match(login):
        raise RuntimeError(f"unexpected issue author login {login!r}")
    body = str(issue.get("body") or "")

    match = ORCID_RE.search(body)
    if not match:
        comment(args.repo, args.issue,
                "I could not find an ORCID iD in this issue. It looks like `0000-0002-1825-0097`; edit the issue "
                "and put yours in the \"ORCID iD\" field to trigger a re-check.", args.dry_run)
        return 0
    orcid = match.group(0)
    if not orcid_checksum_ok(orcid):
        comment(args.repo, args.issue,
                f"`{orcid}` is not a valid ORCID iD (the check digit does not match). Please check it against "
                "your ORCID record and edit the issue to trigger a re-check.", args.dry_run)
        return 0

    existing = find_person(root, login)
    if existing is not None:
        existing_path, existing_data = existing
        if str(existing_data.get("orcid", "")) == orcid:
            comment(args.repo, args.issue, f"`{login}` is already registered with ORCID iD {orcid} "
                    f"(`{existing_path.relative_to(root)}`). Nothing to do.", args.dry_run)
            close_issue(args.repo, args.issue, args.dry_run)
            return 0
        comment(args.repo, args.issue,
                f"`{login}` is already registered with a different ORCID iD ({existing_data.get('orcid', '?')}) in "
                f"`{existing_path.relative_to(root)}`. A maintainer must resolve this; the issue stays open.", args.dry_run)
        return 0

    try:
        urls = researcher_urls(args.orcid_api, orcid)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            comment(args.repo, args.issue, f"There is no public ORCID record for `{orcid}`. Please check the iD and "
                    "edit the issue to trigger a re-check.", args.dry_run)
            return 0
        log(f"ORCID API returned HTTP {error.code}")
        comment(args.repo, args.issue, "I could not reach ORCID, please try again later by editing the issue.", args.dry_run)
        return 0
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        log(f"ORCID API unreachable: {error}")
        comment(args.repo, args.issue, "I could not reach ORCID, please try again later by editing the issue.", args.dry_run)
        return 0

    if not github_link_present(urls, login):
        log(f"github.com/{login} not among {urls}")
        comment(args.repo, args.issue, failure_message(login, orcid, urls), args.dry_run)
        return 0

    name = person_name(args.orcid_api, orcid)
    path = write_person(root, login, orcid, name)
    log(f"wrote {path.relative_to(root)}")
    commit_and_push(root, path, login, args.dry_run)
    shown = f" ({name})" if name else ""
    comment(args.repo, args.issue,
            f"Registered: GitHub account `{login}` is now linked to ORCID iD {orcid}{shown}. "
            "Agents you run can submit proofs as pull requests from this account.", args.dry_run)
    close_issue(args.repo, args.issue, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
