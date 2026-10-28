#!/usr/bin/env python3
"""Validate a pull request that adds links to README.md.

Exit code 0 = every check passed (safe to merge automatically), 1 = something failed.
The pull request's code is never checked out or executed: only its diff and file
list are read through the GitHub API, so untrusted PRs cannot run anything here.
A human-readable report is written to report.md.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

MAX_NEW_LINKS = 10
UA = "Mozilla/5.0 (compatible; awesome-links-checker/1.0)"

# - [Name](https://example.com) — short description.
ENTRY = re.compile(
    r"^- \[(?P<name>[^\]\n]{1,60})\]\((?P<url>https://[^)\s]+)\) [—–-] (?P<desc>.{5,300})$"
)
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly"}


def safe(text, limit=120):
    """Make user-supplied text harmless inside a Markdown comment."""
    return text.replace("`", "'").replace("@", "@​")[:limit]


def norm_host(host):
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def norm(url):
    p = urlparse(url)
    return norm_host(p.hostname or "") + p.path.rstrip("/")


def check_host(host):
    """Return an error string if the domain is unusable, else None."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return "domain does not resolve"
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            return "domain points to a non-public address"
    return None


def reach(url):
    """Return (ok, detail). 401/403/429 count as 'exists' because many sites block bots."""
    last = "no response"
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                return True, f"HTTP {exc.code}, site blocks bots but exists"
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = type(exc).__name__
    return False, last


def parse_diff(diff):
    """Return {filename: [hunk, ...]} where a hunk is a list of (kind, text); kind is '+', '-' or ' '."""
    files = {}
    cur = None
    hunk = None
    for line in diff.rstrip("\n").split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+) b/(.+)$", line)
            cur = files.setdefault(m.group(2) if m else line, [])
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("@@"):
            hunk = []
            cur.append(hunk)
            continue
        if hunk is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            hunk.append(("+", line[1:]))
        elif line.startswith("-"):
            hunk.append(("-", line[1:]))
        else:
            hunk.append((" ", line[1:] if line.startswith(" ") else line))
    return files


def added_runs(hunk):
    """Yield (start, end) index pairs of consecutive added lines."""
    i = 0
    while i < len(hunk):
        if hunk[i][0] == "+":
            j = i
            while j + 1 < len(hunk) and hunk[j + 1][0] == "+":
                j += 1
            yield i, j
            i = j + 1
        else:
            i += 1


def analyze(diff, changed, base_readme, reach_fn=reach, resolve_fn=check_host):
    """Return (ok, report_lines)."""
    lines = []
    state = {"ok": True}

    def fail(msg):
        state["ok"] = False
        lines.append(f"- ❌ {msg}")

    def good(msg):
        lines.append(f"- ✅ {msg}")

    # 1. only README.md may change
    bad = [f"`{safe(n)}`" for n, status in changed if n != "README.md" or status != "modified"]
    if bad or not changed:
        fail("Only edits to `README.md` are allowed. "
             + ("Found: " + ", ".join(bad) if bad else "No changes found."))
        return False, lines
    good("Only `README.md` was changed")

    # 2. only additions
    hunks = parse_diff(diff).get("README.md", [])
    removed = sum(1 for h in hunks for kind, _ in h if kind == "-")
    if removed:
        fail(f"{removed} existing line(s) were removed or edited. Only adding new links is allowed.")
        return False, lines

    # 3. collect added lines, remembering whether they sit next to an existing list item
    entries = []
    for hunk in hunks:
        for i, j in added_runs(hunk):
            neighbours = [hunk[i - 1] if i > 0 else None, hunk[j + 1] if j + 1 < len(hunk) else None]
            placed = any(n and n[0] == " " and n[1].startswith("- ") for n in neighbours)
            for k in range(i, j + 1):
                text = hunk[k][1]
                if text.strip():
                    entries.append((text, placed))
    if not entries:
        fail("No new links found in the changes.")
        return False, lines
    if len(entries) > MAX_NEW_LINKS:
        fail(f"Too many new lines ({len(entries)}). Please add at most {MAX_NEW_LINKS} links per pull request.")
        return False, lines

    # 4. per-link checks
    existing = {norm(u) for u in re.findall(r"\]\((https?://[^)\s]+)\)", base_readme)}
    seen = set()
    for text, placed in entries:
        m = ENTRY.match(text)
        if not m:
            fail("Line does not match the format `- [Name](https://url) — description`: "
                 f"`{safe(text)}`")
            continue
        name, url = m["name"], m["url"]
        errs = []
        detail = ""

        if not placed:
            errs.append("must be added inside an existing list of links")

        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.username or p.password:
            errs.append("URL must not contain credentials")
        elif "." not in host or re.fullmatch(r"[\d.]+", host) or ":" in host:
            errs.append("URL must use a normal domain name")
        elif norm_host(host) in SHORTENERS:
            errs.append("URL shorteners are not allowed")

        t = re.fullmatch(r"(?:https?://)?([\w.-]+\.[a-z]{2,})(?:/\S*)?", name.strip(), re.I)
        if t and norm_host(t.group(1)) != norm_host(host):
            errs.append("link text looks like a different website than the URL")

        key = norm(url)
        if key in existing or key in seen:
            errs.append("already in the list")
        seen.add(key)

        if not errs:
            host_err = resolve_fn(host)
            if host_err:
                errs.append(host_err)
            else:
                reachable, detail = reach_fn(url)
                if not reachable:
                    errs.append(f"link is not reachable ({detail})")

        if errs:
            fail(f"`{safe(name, 60)}`: " + "; ".join(errs))
        else:
            good(f"`{safe(name, 60)}`: format OK, not a duplicate, link is reachable ({detail})")

    return state["ok"], lines


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def write_report(ok, lines):
    head = "### 🤖 Automatic link check\n\n"
    if ok:
        foot = "\n\nAll checks passed. Merging automatically."
    else:
        foot = ("\n\nSome checks failed, so this pull request was **not** merged automatically. "
                "Please fix the points above and push again. A maintainer may also review it manually.")
    with open("report.md", "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + foot + "\n")


def main():
    repo = os.environ["REPO"]
    pr = os.environ["PR_NUMBER"]
    sha = os.environ["HEAD_SHA"]

    try:
        meta = json.loads(gh("api", f"repos/{repo}/pulls/{pr}"))
        if meta["head"]["sha"] != sha:
            write_report(False, ["- ❌ The pull request changed while it was being checked. It will be re-checked."])
            sys.exit(1)
        listing = gh("api", f"repos/{repo}/pulls/{pr}/files", "--paginate",
                     "--jq", '.[] | [.filename, .status] | @tsv')
        changed = [tuple(row.split("\t")) for row in listing.splitlines() if row]
        diff = gh("api", "-H", "Accept: application/vnd.github.v3.diff", f"repos/{repo}/pulls/{pr}")
    except subprocess.CalledProcessError:
        write_report(False, ["- ❌ Could not read the changes (the pull request may be too large)."])
        sys.exit(1)

    with open("README.md", encoding="utf-8") as f:
        base_readme = f.read()

    ok, lines = analyze(diff, changed, base_readme)
    write_report(ok, lines)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()