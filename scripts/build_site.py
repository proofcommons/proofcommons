#!/usr/bin/env python3
"""Build the static Proof Commons web site.

Reads the repository (README.md, AGENTS.md, epoch.toml, Statements/, people/,
ledger/, Proofs/) and writes plain HTML pages.  Standard library only.

    python3 scripts/build_site.py [--root DIR] [--out DIR] [--repo OWNER/NAME]

The output directory defaults to <root>/_site and is deployed to GitHub Pages.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import tomllib
from pathlib import Path

SITE_NAME = "Proof Commons"
MATHLIB_URL = "https://github.com/leanprover-community/mathlib4"


# ---------------------------------------------------------------------------
# Markdown -> HTML.  Small on purpose: it has to render README.md and AGENTS.md,
# not the whole CommonMark specification.
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+.-]*).*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^ {0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~<])")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)")
_AUTOLINK = re.compile(r"&lt;(https?://(?:(?!&gt;)[^\s<>])+)&gt;")
_BARE_URL = re.compile(r"(?<![\w/])https?://(?:(?!&quot;|&#x27;|&lt;|&gt;)[^\s<>\"'])+")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_EM = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])|(?<!\w)_(?=\S)([^_]+?)(?<=\S)_(?!\w)")
_DEL = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def _emph(text: str) -> str:
    text = _STRONG.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _EM.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    return _DEL.sub(r"<del>\1</del>", text)


def _inline(text: str) -> str:
    """Render inline Markdown; everything not recognised is HTML-escaped."""
    stash: list[str] = []

    def keep(s: str) -> str:  # protect finished HTML from later passes
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"


    def bare(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:!?")
        if url.endswith(")") and "(" not in url:
            url = url[:-1]
        return keep(f'<a href="{url}">{url}</a>') + m.group(0)[len(url):]

    text = text.replace("\x00", "")
    text = _ESCAPE.sub(lambda m: keep(html.escape(m.group(1), quote=False)), text)
    text = _CODE.sub(lambda m: keep("<code>" + html.escape(m.group(2).strip()) + "</code>"), text)
    text = html.escape(text)  # from here on ", <, >, & are entities
    text = _IMAGE.sub(lambda m: keep(f'<img src="{m.group(2)}" alt="{m.group(1)}">'), text)
    text = _LINK.sub(lambda m: keep(f'<a href="{m.group(2)}">{_emph(m.group(1))}</a>'), text)
    text = _AUTOLINK.sub(lambda m: keep(f'<a href="{m.group(1)}">{m.group(1)}</a>'), text)
    text = _BARE_URL.sub(bare, text)
    text = _emph(text)
    text = re.sub(r"(?: {2,}|\\)\n", "<br>\n", text)
    for idx in range(len(stash) - 1, -1, -1):
        text = text.replace(f"\x00{idx}\x00", stash[idx])
    return text


def _starts_block(line: str) -> bool:
    if _HEADING.match(line) or _FENCE.match(line) or _HR.match(line) or _QUOTE.match(line):
        return True
    m = _LIST.match(line)  # only "1." may interrupt a paragraph, like CommonMark
    return bool(m and (not m.group(2)[0].isdigit() or m.group(2)[:-1] == "1"))


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _table(lines: list[str], i: int, out: list[str]) -> int:
    header = _split_row(lines[i])
    aligns = []
    for c in _split_row(lines[i + 1]):
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left" if left else "")
    j = i + 2
    rows = []
    while j < len(lines) and lines[j].strip() and "|" in lines[j]:
        rows.append(_split_row(lines[j]))
        j += 1

    def cell(tag: str, k: int, text: str) -> str:
        style = f' style="text-align:{aligns[k]}"' if k < len(aligns) and aligns[k] else ""
        return f"<{tag}{style}>{_inline(text)}</{tag}>"

    head = "".join(cell("th", k, c) for k, c in enumerate(header))
    body = []
    for r in rows:
        r = (r + [""] * len(header))[: len(header)]
        body.append("<tr>" + "".join(cell("td", k, c) for k, c in enumerate(r)) + "</tr>")
    out.append('<div class="tablewrap"><table>\n<thead><tr>' + head + "</tr></thead>\n<tbody>\n"
               + "\n".join(body) + "\n</tbody></table></div>")
    return j


def _list(lines: list[str], i: int, out: list[str]) -> int:
    m = _LIST.match(lines[i])
    indent, marker = len(m.group(1)), m.group(2)
    ordered = marker[0].isdigit()
    items: list[list[str]] = []
    n, loose, blank_before = len(lines), False, False
    while i < n:
        m = _LIST.match(lines[i])
        if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
            break
        loose, blank_before = loose or blank_before, False
        col = len(lines[i]) - len(m.group(3))  # column where the item's content starts
        item = [m.group(3)]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():  # blank line: keep it if the item continues afterwards
                k = i
                while k < n and not lines[k].strip():
                    k += 1
                if k < n and len(lines[k]) - len(lines[k].lstrip()) > indent:
                    item.extend([""] * (k - i))
                    i = k
                    continue
                i, blank_before = k, True  # skip the blanks; the outer loop decides if the list goes on
                break
            ind = len(line) - len(line.lstrip())
            if ind >= col:
                item.append(line[col:])
            elif ind > indent:  # under-indented nested list or continuation
                item.append(line[ind:])
            elif _LIST.match(line) or _starts_block(line) or not item[-1].strip():
                break  # next item, new block, or the list ended after a blank line
            else:  # lazy continuation
                item.append(line.strip())
            i += 1
        items.append(item)
    loose = loose or any("" in item for item in items)
    rendered = []
    for item in items:
        inner = _blocks(item)
        if not loose and inner.startswith("<p>"):  # tight list: no paragraph wrapper
            inner = inner[3:].replace("</p>", "", 1)
        rendered.append(f"<li>{inner}</li>")
    tag = "ol" if ordered else "ul"
    start = f' start="{int(marker[:-1])}"' if ordered and int(marker[:-1]) != 1 else ""
    out.append(f"<{tag}{start}>\n" + "\n".join(rendered) + f"\n</{tag}>")
    return i


def _blocks(lines: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if m := _FENCE.match(line):
            close = re.compile(r"^ {0,3}" + re.escape(m.group(1)) + r"\s*$")
            j = i + 1
            while j < n and not close.match(lines[j]):
                j += 1
            cls = f' class="language-{html.escape(m.group(2))}"' if m.group(2) else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(lines[i + 1:j]))}\n</code></pre>")
            i = j + 1
        elif m := _HEADING.match(line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif _HR.match(line):
            out.append("<hr>")
            i += 1
        elif _QUOTE.match(line):
            quoted = []
            while i < n and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>\n" + _blocks(quoted) + "\n</blockquote>")
        elif _LIST.match(line):
            i = _list(lines, i, out)
        elif "|" in line and i + 1 < n and "|" in lines[i + 1] and _TABLE_SEP.match(lines[i + 1]):
            i = _table(lines, i, out)
        else:  # paragraph (or setext heading)
            j = i + 1
            while j < n and lines[j].strip() and not _starts_block(lines[j]) and not (
                "|" in lines[j] and j + 1 < n and "|" in lines[j + 1] and _TABLE_SEP.match(lines[j + 1])
            ) and not (j == i + 1 and _SETEXT.match(lines[j])):
                j += 1
            if j == i + 1 and j < n and (m := _SETEXT.match(lines[j])):
                level = 1 if m.group(1)[0] == "=" else 2
                out.append(f"<h{level}>{_inline(line.strip())}</h{level}>")
                i = j + 1
                continue
            text = "\n".join(l.lstrip() for l in lines[i:j]).rstrip()
            out.append(f"<p>{_inline(text)}</p>")
            i = j
    return "\n".join(out)


def _strip_comments(text: str) -> str:
    """Remove HTML comments outside fenced code blocks (multi-line ones too)."""
    out, buf, fence = [], [], None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None and m:
            out.append(_COMMENT.sub("", "\n".join(buf)))
            buf, fence = [], re.compile(r"^ {0,3}" + re.escape(m.group(1)) + r"\s*$")
            out.append(line)
        elif fence is not None:
            out.append(line)
            if fence.match(line):
                fence = None
        else:
            buf.append(line)
    out.append(_COMMENT.sub("", "\n".join(buf)))
    return "\n".join(out)


def md_to_html(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _blocks(_strip_comments(text).split("\n"))


# ---------------------------------------------------------------------------
# Repository data
# ---------------------------------------------------------------------------

def warn(msg: str) -> None:
    print(f"build_site: warning: {msg}", file=sys.stderr)


def read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def fmt_date(v) -> str:
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v) if v is not None else ""


def as_list(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)] if v else []


def load_epoch(root: Path) -> dict:
    epoch = {"name": "unknown", "lean": "unknown", "mathlib": "unknown", "mathlib_commit": "", "allowed_axioms": []}
    path = root / "epoch.toml"
    if path.is_file():
        try:
            epoch.update(read_toml(path))
        except Exception as e:  # noqa: BLE001
            warn(f"cannot parse {path}: {e}")
    else:
        warn("epoch.toml is missing")
    return epoch


def load_statements(root: Path) -> list[dict]:
    out = []
    d = root / "Statements"
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.toml")):
        try:
            meta = read_toml(path)
        except Exception as e:  # noqa: BLE001
            warn(f"cannot parse {path}: {e}")
            continue
        sid = path.stem
        if meta.get("id") and meta["id"] != sid:
            warn(f"{path}: id = {meta['id']!r} does not match the file name")
        out.append({
            "id": sid,
            "title": str(meta.get("title") or sid),
            "lean_name": str(meta.get("lean_name") or ""),
            "informal": str(meta.get("informal") or "").strip("\n"),
            "source": str(meta.get("source") or ""),
            "status": str(meta.get("status") or "open"),
            "issue": int(meta.get("issue") or 0),
            "added": fmt_date(meta.get("added")),
            "reviewed_by": as_list(meta.get("reviewed_by")),
            "lean": read_text(d / f"{sid}.lean"),
            "ledger": load_ledger(root, sid),
        })
    out.sort(key=lambda s: (s["title"].lower(), s["id"]))
    return out


def load_ledger(root: Path, sid: str) -> list[dict]:
    out = []
    d = root / "ledger" / sid
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.toml")):
        try:
            e = read_toml(path)
        except Exception as e:  # noqa: BLE001
            warn(f"cannot parse {path}: {e}")
            continue
        name = path.stem
        out.append({
            "name": name,
            "file": str(e.get("file") or f"Proofs/{sid}/{name}.lean"),
            "module": str(e.get("module") or ""),
            "verdict": str(e.get("verdict") or ""),
            "proves_statement": as_list(e.get("proves_statement")),
            "lemmas": as_list(e.get("lemmas")),
            "github": str(e.get("github") or ""),
            "orcid": str(e.get("orcid") or ""),
            "agent": str(e.get("agent") or "").strip(),
            "pr": int(e.get("pr") or 0),
            "merged": fmt_date(e.get("merged")),
            "epoch": str(e.get("epoch") or ""),
            "explanation": read_text(root / "Proofs" / sid / f"{name}.md"),
        })
    out.sort(key=lambda e: (e["merged"], e["name"]))
    return out


def load_people(root: Path) -> list[dict]:
    out = []
    d = root / "people"
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.toml")):
        try:
            p = read_toml(path)
        except Exception as e:  # noqa: BLE001
            warn(f"cannot parse {path}: {e}")
            continue
        out.append({
            "github": str(p.get("github") or path.stem),
            "orcid": str(p.get("orcid") or ""),
            "name": str(p.get("name") or "").strip(),
            "registered": fmt_date(p.get("registered")),
        })
    out.sort(key=lambda p: (p["registered"], p["github"].lower()))
    return out


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
body { font-family: Georgia, "Times New Roman", serif; font-size: 1.05rem; line-height: 1.5;
  color: #111; background: #fff; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; }
h1, h2, h3, h4 { font-family: inherit; font-weight: 600; line-height: 1.3; margin: 1.5em 0 0.5em; }
h1 { font-size: 1.6rem; margin-top: 0.5em; }
h2 { font-size: 1.3rem; }
h3 { font-size: 1.1rem; }
h4 { font-size: 1rem; }
a { color: #0645ad; text-decoration: underline; }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.9em; }
pre { border: 1px solid #ddd; padding: 0.6rem; overflow-x: auto; background: #fafafa; line-height: 1.4; }
pre code { font-size: inherit; }
blockquote { margin: 1em 0; padding-left: 1rem; border-left: 3px solid #ddd; color: #444; }
hr { border: 0; border-top: 1px solid #ddd; margin: 2em 0; }
img { max-width: 100%; }
table { border-collapse: collapse; margin: 0.5em 0 1em; }
th, td { padding: 0.3rem 0.6rem; border-bottom: 1px solid #ddd; text-align: left; vertical-align: top; }
th { font-weight: 600; }
.tablewrap { overflow-x: auto; }
table.meta th { padding-left: 0; white-space: nowrap; }
.informal, .prewrap { white-space: pre-wrap; }
header nav { font-size: 0.95rem; border-bottom: 1px solid #ddd; padding-bottom: 0.5rem; margin-bottom: 1.5rem; }
header nav .site { font-weight: 600; }
footer { font-size: 0.85rem; color: #555; border-top: 1px solid #ddd; margin-top: 3rem; padding-top: 0.6rem; }
footer p { margin: 0 0 0.3em; }
.verified { color: #2a7a2a; }
"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def link(url: str, text: str) -> str:
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def lean_short(lean: str) -> str:
    """'leanprover/lean4:v4.33.1' -> 'v4.33.1' (footer only)."""
    return lean.rsplit(":", 1)[-1] if ":" in lean else lean


class Site:
    def __init__(self, root: Path, out: Path, repo: str):
        self.root, self.out, self.repo = root, out, repo
        self.gh = f"https://github.com/{repo}"
        self.epoch = load_epoch(root)
        self.statements = load_statements(root)
        self.people = load_people(root)
        self.build_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
        self.written: list[Path] = []

    # -- shell -------------------------------------------------------------
    def page(self, title: str, body: str, rel: str = "") -> str:
        full = SITE_NAME if title == SITE_NAME else f"{title} · {SITE_NAME}"
        nav = " · ".join([
            f'<a class="site" href="{rel}index.html">{SITE_NAME}</a>',
            f'<a href="{rel}index.html#statements">Statements</a>',
            f'<a href="{rel}people.html">People</a>',
            f'<a href="{rel}agents.html">For agents</a>',
            f'<a href="{rel}verification.html">Verification</a>',
            f'<a href="{esc(self.gh)}">Source</a>',
        ])
        e = self.epoch
        footer = (f"<p>Epoch {esc(e['name'])} · Lean {esc(lean_short(str(e['lean'])))} · Mathlib {esc(e['mathlib'])}</p>\n"
                  f"<p>Code and Lean files: Apache-2.0. Text: CC BY 4.0. Built {self.build_date} (UTC).</p>")
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{esc(full)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            f"<header><nav>{nav}</nav></header>\n<main>\n{body}\n</main>\n"
            f"<footer>\n{footer}\n</footer>\n</body>\n</html>\n"
        )

    def write(self, relpath: str, content: str) -> None:
        path = self.out / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.written.append(path)

    # -- helpers -----------------------------------------------------------
    _PAGES = {"README.md": "index.html", "AGENTS.md": "agents.html"}
    _ATTR = re.compile(r'(href|src)="([^"]*)"')

    def rewrite_links(self, rendered: str, rel: str = "") -> str:
        """Point relative links in rendered Markdown at site pages or at GitHub."""
        def sub(m: re.Match) -> str:
            kind, url = m.group(1), m.group(2)
            if not url or re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//|#)", url):
                return m.group(0)
            path, _, frag = url.partition("#")
            path = re.sub(r"^(?:\./)+", "", path)
            frag = f"#{frag}" if frag else ""
            if path in self._PAGES:
                new = rel + self._PAGES[path]
            elif kind == "src":
                new = f"https://raw.githubusercontent.com/{self.repo}/main/{path}"
            else:
                new = f"{self.gh}/{'tree' if path.endswith('/') else 'blob'}/main/{path.rstrip('/')}"
            return f'{kind}="{new}{frag}"'
        return self._ATTR.sub(sub, rendered)

    def status_html(self, status: str) -> str:
        if status == "proved":
            return '<span class="verified">✓</span> verified'
        return esc(status)

    def orcid_link(self, orcid: str) -> str:
        return link(f"https://orcid.org/{orcid}", orcid)

    def issue_link(self, issue: int) -> str:
        return link(f"{self.gh}/issues/{issue}", f"#{issue}") if issue else ""

    # -- pages -------------------------------------------------------------
    def build(self) -> None:
        self.write("index.html", self.page(SITE_NAME, self.index_body()))
        for s in self.statements:
            self.write(f"statements/{s['id']}.html", self.page(s["title"], self.statement_body(s), rel="../"))
        self.write("people.html", self.page("People", self.people_body()))
        self.write("agents.html", self.page("For agents", self.agents_body()))
        self.write("verification.html", self.page("Verification", self.verification_body()))
        self.write(".nojekyll", "")

    def index_body(self) -> str:
        readme = read_text(self.root / "README.md")
        parts = [self.rewrite_links(md_to_html(readme)) if readme is not None else f"<h1>{SITE_NAME}</h1>"]
        parts.append('<h2 id="statements">Statements</h2>')
        if not self.statements:
            parts.append("<p>There are no statements yet.</p>")
            return "\n".join(parts)
        rows = []
        for s in self.statements:
            rows.append("<tr>"
                        f"<td>{link(f'statements/{s['id']}.html', s['title'])}</td>"
                        f"<td>{self.status_html(s['status'])}</td>"
                        f"<td>{len(s['ledger'])}</td>"
                        f"<td>{self.issue_link(s['issue']) or '—'}</td>"
                        "</tr>")
        parts.append('<div class="tablewrap"><table>\n<thead><tr><th>Statement</th><th>Status</th>'
                     '<th>Verified proofs</th><th>Thread</th></tr></thead>\n<tbody>\n'
                     + "\n".join(rows) + "\n</tbody></table></div>")
        return "\n".join(parts)

    def statement_body(self, s: dict) -> str:
        sid = s["id"]
        p = [f"<h1>{esc(s['title'])}</h1>"]
        if s["informal"]:
            p.append(f'<div class="informal">{esc(s["informal"])}</div>')
        p.append("<h2>Formal statement</h2>")
        if s["lean"] is not None:
            p.append(f"<pre><code class=\"language-lean\">{esc(s['lean'].rstrip())}\n</code></pre>")
        else:
            p.append(f"<p>The file <code>Statements/{esc(sid)}.lean</code> is missing.</p>")
        reviewed = (", ".join(self.orcid_link(o) for o in s["reviewed_by"])
                    if s["reviewed_by"] else "The formalization has not been reviewed yet.")
        thread = self.issue_link(s["issue"]) or "not opened yet"
        rows = [
            ("Status", self.status_html(s["status"])),
            ("Added", esc(s["added"])),
            ("Source", esc(s["source"])),
            ("Lean name", f"<code>{esc(s['lean_name'])}</code>" if s["lean_name"] else ""),
            ("Formalization reviewed by", reviewed),
            ("Thread", thread),
            ("Lean file", link(f"{self.gh}/blob/main/Statements/{sid}.lean", f"Statements/{sid}.lean")),
        ]
        p.append("<h2>Metadata</h2>")
        p.append(self.meta_table(rows))
        p.append("<h2>Verified proofs</h2>")
        if not s["ledger"]:
            p.append("<p>No verified proofs yet.</p>")
        for e in s["ledger"]:
            p.append(self.ledger_entry(e))
        return "\n".join(p)

    @staticmethod
    def meta_table(rows: list[tuple[str, str]]) -> str:
        body = "\n".join(f'<tr><th scope="row">{esc(k)}</th><td>{v}</td></tr>' for k, v in rows if v)
        return f'<div class="tablewrap"><table class="meta">\n{body}\n</table></div>'

    def ledger_entry(self, e: dict) -> str:
        verdict = {"statement": "proves the statement", "lemmas": "verified lemmas only"}.get(e["verdict"], e["verdict"])
        decls = lambda xs: ", ".join(f"<code>{esc(x)}</code>" for x in xs)  # noqa: E731
        submitted = link(f"https://github.com/{e['github']}", e["github"]) if e["github"] else ""
        if e["orcid"]:
            person = next((p for p in self.people if p["github"] == e["github"] and p["orcid"] == e["orcid"]), None)
            who = f"{esc(person['name'])} ({self.orcid_link(e['orcid'])})" if person and person["name"] else self.orcid_link(e["orcid"])
            submitted += f" for endorser {who}"
        rows = [
            ("Verdict", esc(verdict)),
            ("Proves the statement", decls(e["proves_statement"])),
            ("Lemmas", decls(e["lemmas"])),
            ("Submitted by", submitted),
            ("Agent", f'<span class="prewrap">{esc(e["agent"])}</span>' if e["agent"] else ""),
            ("Pull request", link(f"{self.gh}/pull/{e['pr']}", f"#{e['pr']}") if e["pr"] else ""),
            ("Merged", esc(e["merged"])),
            ("Epoch", esc(e["epoch"])),
        ]
        out = [f"<h3>{link(f'{self.gh}/blob/main/{e['file']}', e['file'])}</h3>", self.meta_table(rows)]
        if e["explanation"] and e["explanation"].strip():
            out.append(f"<h4>Explanation</h4>\n<div class=\"explanation\">\n{self.rewrite_links(md_to_html(e['explanation']), '../')}\n</div>")
        return "\n".join(out)

    def people_body(self) -> str:
        p = ["<h1>People</h1>",
             "<p>Registered endorsers. Every verified proof file on Proof Commons was submitted by one of them, "
             "and each of them is identified by an ORCID iD whose public record lists their GitHub account.</p>"]
        if not self.people:
            p.append("<p>Nobody has registered yet.</p>")
            return "\n".join(p)
        rows = []
        for person in self.people:
            rows.append("<tr>"
                        f"<td>{esc(person['name'] or person['github'])}</td>"
                        f"<td>{self.orcid_link(person['orcid']) if person['orcid'] else ''}</td>"
                        f"<td>{link(f'https://github.com/{person['github']}', person['github'])}</td>"
                        f"<td>{esc(person['registered'])}</td></tr>")
        p.append('<div class="tablewrap"><table>\n<thead><tr><th>Name</th><th>ORCID</th><th>GitHub</th>'
                 '<th>Registered</th></tr></thead>\n<tbody>\n' + "\n".join(rows) + "\n</tbody></table></div>")
        return "\n".join(p)

    def agents_body(self) -> str:
        text = read_text(self.root / "AGENTS.md")
        if text is None:
            return "<h1>For agents</h1>\n<p>The file <code>AGENTS.md</code> has not been written yet.</p>"
        return self.rewrite_links(md_to_html(text))

    def verification_body(self) -> str:
        e = self.epoch
        commit = str(e.get("mathlib_commit") or "")
        commit_html = (f" (commit {link(f'{MATHLIB_URL}/tree/{commit}', commit[:12])})" if commit else "")
        axioms = as_list(e.get("allowed_axioms"))
        axioms_html = ", ".join(f"<code>{esc(a)}</code>" for a in axioms) or "none"
        readme = f"{self.gh}/blob/main/scripts/README.md"
        lean_v = lean_short(str(e["lean"]))
        return f"""<h1>What the verified mark means</h1>
<p>A proof file on Proof Commons carries the mark <span class="verified">✓</span> verified when the Lean
kernel has accepted it as a proof of the registered formal statement. The check is done by a program, not
by a person, and always against the same fixed toolchain, which we call an epoch. Bumping the epoch is a
maintainer decision, and all proofs on <code>main</code> are re-verified when it changes.</p>
<h2>The current epoch</h2>
{self.meta_table([
    ("Epoch", esc(e["name"])),
    ("Lean", f"<code>{esc(e['lean'])}</code>"),
    ("Mathlib", f"<code>{esc(e['mathlib'])}</code>{commit_html}"),
    ("Allowed axioms", axioms_html),
])}
<h2>The checks</h2>
<p>A pull request with a proof is checked in the following order; the first failure rejects it.</p>
<ol>
<li>The pull request may only add or change files of the form <code>Proofs/&lt;Id&gt;/&lt;name&gt;.lean</code>
or <code>Proofs/&lt;Id&gt;/&lt;name&gt;.md</code>, at most five of them and each at most 200 kB, and the
statement <code>Statements/&lt;Id&gt;.lean</code> must already exist on <code>main</code>.</li>
<li>The proof may import only Mathlib, the statements, and proof files that are already on <code>main</code>.</li>
<li>The file may not contain <code>sorry</code>, <code>native_decide</code>, <code>unsafe</code>,
<code>implemented_by</code>, <code>extern</code>, or any of the other escape hatches listed in
{link(readme, "scripts/README.md")}, which would let a proof bypass the kernel.</li>
<li>The module must compile with <code>lake build</code> inside the verifier container, which runs without
network access and under memory and time limits.</li>
<li>A generated check file imports the module and, for every declaration in it, records its kind, whether its
type is definitionally equal to the registered statement, and the axioms it depends on; every declaration
must live in the namespace <code>ProofCommons.&lt;Id&gt;.&lt;name&gt;</code> and every axiom must be one of
{axioms_html}.</li>
<li>The verdict is <em>proves the statement</em> if some declaration has the type of the registered statement,
<em>verified lemmas only</em> if at least one theorem was verified without proving the statement, and
<em>rejected</em> if nothing was proved.</li>
</ol>
<p>The verifier does not yet replay the compiled <code>.olean</code> files through the independent kernel
checker <code>lean4checker</code>; this is planned as soon as a release of <code>lean4checker</code> exists
for the pinned toolchain (there was none for Lean {esc(lean_v)} at the time of writing).</p>
<h2>What it does not mean</h2>
<p>The mark says that the Lean kernel accepted a proof of the formal statement in
<code>Statements/&lt;Id&gt;.lean</code>. It does not say that this formal statement is the theorem the
informal text describes. Whether a formalization is faithful is a human judgement, which is why every
statement page lists the ORCID iDs of the people who reviewed the formalization, or says plainly that nobody
has reviewed it yet. Nor does the mark say anything about the elegance or the length of a proof; a proof file
is either accepted by the kernel or it is not.</p>
<p>Every verified file is also tied to a person: the author of the pull request must be a registered
endorser whose public ORCID record lists their GitHub account. The statement page names the endorser next to
each proof, and the {link("people.html", "people page")} lists all registered endorsers.</p>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Proof Commons static site.")
    default_root = Path(__file__).resolve().parent.parent
    ap.add_argument("--root", type=Path, default=default_root, help="repository root (default: parent of scripts/)")
    ap.add_argument("--out", type=Path, default=None, help="output directory (default: <root>/_site)")
    ap.add_argument("--repo", default="proofcommons/proofcommons", help="GitHub repository OWNER/NAME")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        ap.error("--repo must have the form OWNER/NAME")
    root = args.root.resolve()
    out = (args.out if args.out is not None else root / "_site").resolve()
    if not root.is_dir():
        ap.error(f"root {root} is not a directory")
    site = Site(root, out, args.repo)
    site.build()
    for path in site.written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
