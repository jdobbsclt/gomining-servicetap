#!/usr/bin/env python3
"""Build the one-line bookmarklet from bookmarklet.src.js and stamp it into the pages.

    python setup-wizard/bookmarklet/build.py           # rewrite the pages
    python setup-wizard/bookmarklet/build.py --check   # exit 1 if a page is out of date

The bookmarklet appears on two pages (the setup wizard and the standalone cookie tool);
building both from one source means they can never drift apart. Needs `node` on PATH
only for a syntax check of the result.
"""
import html
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SRC = HERE / "bookmarklet.src.js"
TARGETS = [ROOT / "docs" / "setup.html", ROOT / "docs" / "cookie-tool.html"]

# <a ... id="bookmarklet-link" ... href="ANYTHING"> -- we replace ANYTHING.
ANCHOR = re.compile(r'(<a\b[^>]*\bid="bookmarklet-link"[^>]*\bhref=")[^"]*(")')


def build_code():
    lines = []
    for raw in SRC.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        lines.append(stripped)
    code = " ".join(lines)
    if '"' in code:
        sys.exit("bookmarklet source must not contain double quotes (it lives in a double-quoted attribute)")
    if not code.isascii():
        sys.exit("bookmarklet source must be pure ASCII (write non-ASCII characters as backslash-u escapes) "
                 "so it survives being exported/imported as a browser bookmark")
    return code


def syntax_check(code):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("bookmarklet has a syntax error after collapsing to one line:\n" + result.stderr)


def main():
    check_only = "--check" in sys.argv
    code = build_code()
    syntax_check(code)
    href = "javascript:" + html.escape(code, quote=False)
    stale = False
    for target in TARGETS:
        text = target.read_text(encoding="utf-8")
        if len(ANCHOR.findall(text)) != 1:
            sys.exit(f"{target.name}: expected exactly one <a id=\"bookmarklet-link\" href=...>")
        new_text = ANCHOR.sub(lambda m: m.group(1) + href + m.group(2), text)
        if new_text != text:
            stale = True
            if not check_only:
                target.write_text(new_text, encoding="utf-8", newline="")
                print(f"updated {target.name}")
        else:
            print(f"{target.name}: already current")
    if check_only and stale:
        sys.exit("pages are out of date -- run build.py")
    print(f"bookmarklet: {len(code)} characters")


if __name__ == "__main__":
    main()
