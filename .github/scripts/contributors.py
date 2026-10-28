#!/usr/bin/env python3
"""Generate one round (circle-clipped) SVG avatar per contributor in
assets/avatars/ and refresh the block between the CONTRIBUTORS markers
in README.md. GitHub strips CSS from READMEs, so rounding is baked into
the images themselves."""

import base64
import glob
import os
import re
import subprocess
import sys
import urllib.request

REPO = os.environ["REPO"]
MAX = int(os.environ.get("MAX", "20"))
SIZE = 64

START = "<!-- CONTRIBUTORS:START -->"
END = "<!-- CONTRIBUTORS:END -->"
AVATAR_DIR = "assets/avatars"


def contributors():
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contributors?per_page=100", "--paginate",
         "--jq", '.[] | select(.type=="User") | .login'],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.split() if line]


def avatar_data_uri(login):
    req = urllib.request.Request(
        f"https://github.com/{login}.png?size={SIZE * 2}",
        headers={"User-Agent": "contributors-script"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        mime = resp.headers.get_content_type() or "image/png"
        data = resp.read()
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def round_svg(uri):
    r = SIZE / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
        f'viewBox="0 0 {SIZE} {SIZE}">\n'
        f'<clipPath id="c"><circle cx="{r}" cy="{r}" r="{r}"/></clipPath>\n'
        f'<image href="{uri}" width="{SIZE}" height="{SIZE}" clip-path="url(#c)"/>\n'
        "</svg>\n"
    )


def main():
    logins = contributors()
    shown = logins[:MAX]
    rest = len(logins) - len(shown)

    os.makedirs(AVATAR_DIR, exist_ok=True)
    done = []
    for login in shown:
        try:
            svg = round_svg(avatar_data_uri(login))
        except Exception as exc:  # keep going if a single avatar fails
            print(f"warning: could not fetch avatar for {login}: {exc}", file=sys.stderr)
            continue
        with open(f"{AVATAR_DIR}/{login}.svg", "w", encoding="utf-8") as f:
            f.write(svg)
        done.append(login)
    if not done:
        sys.exit("No avatars could be generated")

    # remove avatars of people who are no longer shown (and the old strip file)
    keep = {f"{AVATAR_DIR}/{login}.svg" for login in done}
    for path in glob.glob(f"{AVATAR_DIR}/*.svg"):
        if path not in keep:
            os.remove(path)
    if os.path.exists("assets/contributors.svg"):
        os.remove("assets/contributors.svg")

    link = f"https://github.com/{REPO}/graphs/contributors"
    cells = "\n".join(
        f'  <a href="https://github.com/{login}">'
        f'<img src="{AVATAR_DIR}/{login}.svg" width="{SIZE}" height="{SIZE}" '
        f'alt="{login}" title="{login}" /></a>'
        for login in done
    )
    block = f'<p align="center">\n{cells}\n</p>'
    if rest > 0:
        block += (
            '\n\n<p align="center">\n'
            f'  <a href="{link}"><strong>+ {rest} more contributors</strong></a>\n'
            "</p>"
        )

    with open("README.md", encoding="utf-8") as f:
        text = f.read()

    pattern = re.escape(START) + r".*?" + re.escape(END)
    if not re.search(pattern, text, flags=re.S):
        sys.exit("Markers CONTRIBUTORS:START / END not found in README.md")

    new = re.sub(pattern, lambda _m: f"{START}\n{block}\n{END}", text, flags=re.S)
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(new)


if __name__ == "__main__":
    main()