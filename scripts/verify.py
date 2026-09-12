#!/usr/bin/env python3
"""Verify proof files submitted to Proof Commons.

The rules are documented in scripts/README.md ("Verification"). In short: a pull
request may only touch Proofs/<Id>/<name>.lean and .md files; each Lean file must
compile on the pinned toolchain, keep every declaration in the namespace
ProofCommons.<Id>.<name>, and depend on no axioms beyond those in epoch.toml.
A file "proves the statement" if some declaration in it has, up to definitional
unfolding, the type ProofCommons.<Id>.Statement.

CI usage (host side, untrusted Lean code runs only inside the container):

    python3 scripts/verify.py --base . --pr pr --files changed.txt \
        --image ghcr.io/proofcommons/verifier:latest --out result.json --report report.md

Local usage, inside the verifier image or any checkout with Mathlib built:

    python3 scripts/verify.py --base . --pr . --files changed.txt --no-docker

Exit code 0 whenever a verdict was produced (including "rejected"); nonzero only
when the verifier itself failed (no Docker, no image, missing files).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PATH_RE = re.compile(r"^Proofs/([A-Za-z0-9_]+)/([A-Za-z][A-Za-z0-9_]*)\.(lean|md)$")
LEAN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*$")
IMPORT_RE = re.compile(r"^\s*import\s+(\S+)", re.MULTILINE)
MAX_FILES = 5
MAX_BYTES = 200_000
FORBIDDEN = [
    (r"\bsorry\b", "`sorry`"),
    (r"\bnative_decide\b", "`native_decide` (evaluates outside the kernel)"),
    (r"\bunsafe\b", "`unsafe`"),
    (r"\bimplemented_by\b", "`implemented_by`"),
    (r"\bextern\b", "`extern`"),
    (r"#eval\b", "`#eval`"),
    (r"\brun_cmd\b", "`run_cmd`"),
    (r"\brun_meta\b", "`run_meta`"),
    (r"\brun_elab\b", "`run_elab`"),
    (r"\binitialize\b", "`initialize`"),
    (r"skipKernelTC", "`debug.skipKernelTC`"),
    (r"addDeclWithoutChecking", "`addDeclWithoutChecking`"),
    (r"\bIO\.Process\b", "`IO.Process`"),
    (r"\bIO\.FS\b", "`IO.FS`"),
]
MARKER = "PROOFCOMMONS_JSON "
LOG_TAIL = 60  # lines of Lean output kept in the report


@dataclass
class LeanFile:
    path: str
    statement: str
    name: str
    module: str
    lean_name: str = ""
    compiled: bool = False
    log: str = ""
    declarations: list[dict] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "path": self.path, "module": self.module, "statement": self.statement,
            "compiled": self.compiled, "log": self.log, "declarations": self.declarations,
        }


class Rejected(Exception):
    """A rule violation that rejects the whole pull request."""


# ----------------------------------------------------------------------------- static checks

def read_files_list(arg: str) -> list[str]:
    p = Path(arg)
    text = p.read_text() if p.exists() else arg
    return [line.strip() for line in text.splitlines() if line.strip()]


def static_checks(base: Path, pr: Path, files: list[str]) -> tuple[list[LeanFile], list[str]]:
    """Apply rules 1-3 of scripts/README.md. Returns (lean files, md files)."""
    if not files:
        raise Rejected("the pull request changes no files")
    if len(files) > MAX_FILES:
        raise Rejected(f"the pull request changes {len(files)} files; the limit is {MAX_FILES}")
    lean_files: list[LeanFile] = []
    md_files: list[str] = []
    for f in files:
        m = PATH_RE.match(f)
        if not m:
            raise Rejected(f"`{f}` is outside `Proofs/<Id>/<name>.lean|md`; only proof files may be changed")
        statement, name, ext = m.groups()
        if not (base / "Statements" / f"{statement}.lean").exists():
            raise Rejected(f"`{f}`: there is no statement `Statements/{statement}.lean` on main")
        src = pr / f
        if not src.exists():
            # A deleted file: nothing to verify, but agents should not delete verified proofs.
            raise Rejected(f"`{f}` was deleted; proofs on main are never removed by pull request")
        if src.stat().st_size > MAX_BYTES:
            raise Rejected(f"`{f}` is larger than {MAX_BYTES // 1000} kB")
        if ext == "md":
            if not (pr / f"Proofs/{statement}/{name}.lean").exists() and \
               not (base / f"Proofs/{statement}/{name}.lean").exists():
                raise Rejected(f"`{f}` explains a proof file `Proofs/{statement}/{name}.lean` that does not exist")
            md_files.append(f)
            continue
        text = src.read_text(errors="replace")
        for pattern, label in FORBIDDEN:
            if re.search(pattern, text):
                raise Rejected(f"`{f}` contains {label}, which is not allowed in proof files")
        for imp in IMPORT_RE.findall(text):
            if imp == "Mathlib" or imp.startswith("Mathlib.") or imp.startswith("Statements."):
                continue
            if imp.startswith("Proofs."):
                rel = Path(*imp.split(".")).with_suffix(".lean")
                if (base / rel).exists():
                    continue
                raise Rejected(f"`{f}` imports `{imp}`, which is not on main yet")
            raise Rejected(f"`{f}` imports `{imp}`; only `Mathlib`, `Statements.*` and merged `Proofs.*` are allowed")
        meta = tomllib.loads((base / "Statements" / f"{statement}.toml").read_text())
        lean_name = meta.get("lean_name", "")
        if not LEAN_NAME_RE.match(lean_name):
            raise Rejected(f"`Statements/{statement}.toml` has an invalid `lean_name`; a maintainer must fix it")
        lean_files.append(LeanFile(path=f, statement=statement, name=name,
                                   module=f"Proofs.{statement}.{name}", lean_name=lean_name))
    if not lean_files and not md_files:
        raise Rejected("nothing to verify")
    return lean_files, md_files


# ----------------------------------------------------------------------------- Lean checks

def check_source(template: Path, lf: LeanFile) -> str:
    return template.read_text().replace("{{MODULE}}", lf.module).replace("{{STATEMENT}}", lf.lean_name)


def stage_proofs(base: Path, pr: Path, lean_files: list[LeanFile], md_files: list[str], stage: Path) -> None:
    """Copy main's Proofs/ and overlay the pull request's files: what the container sees."""
    if (base / "Proofs").exists():
        shutil.copytree(base / "Proofs", stage / "Proofs", dirs_exist_ok=True)
    (stage / "Proofs").mkdir(exist_ok=True)
    for f in [lf.path for lf in lean_files] + md_files:
        dst = stage / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pr / f, dst)


def run_lean(lf: LeanFile, *, base: Path, stage: Path, check_file: Path, image: str | None,
             timeout: int, memory: str, cpus: str) -> tuple[int, str]:
    """Build the module and run Check.lean, inside Docker unless image is None."""
    inner = f"lake build {lf.module} && lake env lean Check.lean"
    if image is None:
        # Local mode: base is the working Lake project; put the files in place.
        for f in (stage / "Proofs").rglob("*"):
            if f.is_file():
                dst = base / f.relative_to(stage)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
        shutil.copyfile(check_file, base / "Check.lean")
        cmd = ["bash", "-c", inner]
        cwd = base
        name = None
    else:
        name = f"pc-verify-{os.getpid()}-{lf.name}"
        cmd = [
            "docker", "run", "--rm", "--name", name, "--network", "none",
            "--memory", memory, "--cpus", cpus, "--pids-limit", "1024",
            "-v", f"{(stage / 'Proofs').resolve()}:/pc/Proofs:ro",
            "-v", f"{(base / 'Statements').resolve()}:/pc/Statements:ro",
            "-v", f"{check_file.resolve()}:/pc/Check.lean:ro",
            image, "bash", "-c", inner,
        ]
        cwd = None
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[verifier] timed out after {timeout} s"
    finally:
        if image is None:
            (base / "Check.lean").unlink(missing_ok=True)


def parse_report(lf: LeanFile, output: str, allowed: set[str]) -> list[str]:
    """Fill lf.declarations from the PROOFCOMMONS_JSON line; return rule violations."""
    payload = None
    for line in output.splitlines():
        if line.startswith(MARKER):
            payload = json.loads(line[len(MARKER):])
    if payload is None:
        return [f"`{lf.path}`: the check produced no report (compile error above)"]
    problems: list[str] = []
    prefix = f"ProofCommons.{lf.statement}.{lf.name}."
    for d in payload["declarations"]:
        bad = sorted(set(d["axioms"]) - allowed)
        d["allowed"] = not bad
        lf.declarations.append(d)
        if not d["name"].startswith(prefix):
            problems.append(f"`{lf.path}`: `{d['name']}` is outside the namespace `{prefix[:-1]}`")
        if bad:
            problems.append(f"`{lf.path}`: `{d['name']}` depends on disallowed axioms: {', '.join(bad)}")
    return problems


def tail(text: str, n: int = LOG_TAIL) -> str:
    lines = [l for l in text.splitlines() if not l.startswith(MARKER)]
    return "\n".join(lines[-n:])


# ----------------------------------------------------------------------------- reporting

def verdict_of(lean_files: list[LeanFile], md_files: list[str], reasons: list[str]) -> str:
    if reasons:
        return "rejected"
    if any(d["proves_statement"] for lf in lean_files for d in lf.declarations):
        return "statement"
    if any(d["kind"] == "theorem" for lf in lean_files for d in lf.declarations):
        return "lemmas"
    if md_files and not lean_files:
        return "lemmas"  # an explanation of an existing proof file; nothing new is proved
    reasons.append("the files compile but contain no theorem")
    return "rejected"


def write_report(path: Path, verdict: str, epoch: str, reasons: list[str],
                 lean_files: list[LeanFile], md_files: list[str]) -> None:
    human = {"statement": "verified, proves the statement",
             "lemmas": "verified, lemmas only", "rejected": "rejected"}[verdict]
    out = [f"**Verdict:** {human} (epoch {epoch})", ""]
    if reasons:
        out.append("**Reasons**")
        out += [f"- {r}" for r in reasons]
        out.append("")
    for lf in lean_files:
        out.append(f"**`{lf.path}`** — {'compiled' if lf.compiled else 'did not compile'}")
        if lf.declarations:
            out += ["", "| declaration | kind | proves the statement | axioms |", "|---|---|---|---|"]
            for d in lf.declarations:
                axs = ", ".join(d["axioms"]) or "none"
                out.append(f"| `{d['name']}` | {d['kind']} | {'yes' if d['proves_statement'] else 'no'} | {axs} |")
        if lf.log.strip():
            out += ["", "<details><summary>Lean output</summary>", "", "```", lf.log, "```", "", "</details>"]
        out.append("")
    for f in md_files:
        out.append(f"**`{f}`** — explanation file, not checked by Lean")
    path.write_text("\n".join(out) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, type=Path, help="trusted checkout of main")
    ap.add_argument("--pr", required=True, type=Path, help="untrusted checkout of the pull request head")
    ap.add_argument("--files", required=True, help="file with changed paths, one per line, or the list itself")
    ap.add_argument("--image", default="ghcr.io/proofcommons/verifier:latest")
    ap.add_argument("--no-docker", action="store_true", help="run lake directly in --base (must have Mathlib built)")
    ap.add_argument("--out", type=Path, default=Path("result.json"))
    ap.add_argument("--report", type=Path, default=Path("report.md"))
    ap.add_argument("--timeout", type=int, default=1200, help="seconds per file")
    ap.add_argument("--memory", default="8g")
    ap.add_argument("--cpus", default="4")
    args = ap.parse_args()

    epoch = tomllib.loads((args.base / "epoch.toml").read_text())
    allowed = set(epoch["allowed_axioms"])
    template = args.base / "scripts" / "check_template.lean"
    files = read_files_list(args.files)

    reasons: list[str] = []
    lean_files: list[LeanFile] = []
    md_files: list[str] = []
    try:
        lean_files, md_files = static_checks(args.base, args.pr, files)
    except Rejected as e:
        reasons.append(str(e))

    if not reasons and lean_files:
        image = None if args.no_docker else args.image
        with tempfile.TemporaryDirectory(prefix="pc-verify-", dir=str(args.base / ".tmp") if (args.base / ".tmp").exists() else None) as td:
            stage = Path(td)
            stage_proofs(args.base, args.pr, lean_files, md_files, stage)
            for lf in lean_files:
                check_file = stage / f"Check_{lf.name}.lean"
                check_file.write_text(check_source(template, lf))
                code, output = run_lean(lf, base=args.base, stage=stage, check_file=check_file, image=image,
                                        timeout=args.timeout, memory=args.memory, cpus=args.cpus)
                lf.log = tail(output)
                lf.compiled = code == 0 and MARKER in output
                if not lf.compiled:
                    reasons.append(f"`{lf.path}` did not compile (exit code {code})")
                    continue
                reasons += parse_report(lf, output, allowed)

    verdict = verdict_of(lean_files, md_files, reasons)
    result = {"verdict": verdict, "epoch": epoch["name"], "reasons": reasons,
              "files": [lf.as_json() for lf in lean_files] + [{"path": f, "kind": "explanation"} for f in md_files]}
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    write_report(args.report, verdict, epoch["name"], reasons, lean_files, md_files)
    print(f"verdict: {verdict}")
    for r in reasons:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"[verifier] infrastructure failure: {exc!r}", file=sys.stderr)
        sys.exit(2)
