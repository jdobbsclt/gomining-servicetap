"""Re-capture GoMining session cookies for one or more accounts and push
them to this repo's GitHub Actions secrets.

Run this locally when a scheduled run reports "session expired" -- a dead
session. GoMining invalidates sessions periodically (e.g. when they change
their auth, which is what happened on 2026-09-06). It opens a real browser
window; you (and any other account owner) complete the "Continue with
Google" login yourselves. This script never sees or stores a password or
2FA code -- only the resulting session cookies.

Requirements:
  - playwright + chromium:
        pip install playwright && playwright install chromium
  - the GitHub CLI, logged in with push access to this repo:
        gh auth status

Usage:
  python recapture.py                 # every account in ACCOUNTS below
  python recapture.py SECONDARY       # just one (space-separated for several)
"""
import json
import subprocess
import sys

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://app.gomining.com/login"
DASHBOARD_URL = "https://app.gomining.com/nft-miners"

# Keep this in sync with KEEP_COOKIE_NAMES in gomining_maintenance.py.
KEEP_COOKIE_NAMES = ["access_token", "refresh_token", "cf_clearance"]

# label -> GitHub secret name. Mirrors GOMINING_ACCOUNT_LABELS and the
# GOMINING_COOKIES_<LABEL> secrets in .github/workflows/maintenance.yml.
# A fork with different account labels edits this one dict.
ACCOUNTS = {
    "PRIMARY": "GOMINING_COOKIES_PRIMARY",
    "SECONDARY": "GOMINING_COOKIES_SECONDARY",
}

# Playwright cookie fields the maintenance script's add_cookies() expects.
_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires",
                  "httpOnly", "secure", "sameSite")


def capture_one(label, secret, context, page):
    context.clear_cookies()
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    input(
        f"\n  >>> A browser window is open. Log in as {label} with "
        f'"Continue with Google",\n      then press Enter here once you '
        f"see the GoMining dashboard... "
    )

    # Land on the exact page the automation uses, then let it settle.
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    cookies = [
        {k: c[k] for k in _COOKIE_FIELDS}
        for c in context.cookies()
        if "gomining.com" in c["domain"] and c["name"] in KEEP_COOKIE_NAMES
    ]
    names = sorted(c["name"] for c in cookies)

    if "access_token" not in names or "refresh_token" not in names:
        print(f"  ! {label}: not logged in (found {names or 'no session cookies'}). Skipped.")
        return False

    result = subprocess.run(
        ["gh", "secret", "set", secret, "--body", json.dumps(cookies)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ! {label}: `gh secret set` failed -- {result.stderr.strip()}")
        print("      (wrong GitHub account active? try:  gh auth switch)")
        return False

    print(f"  OK {label}: saved {names} to {secret}")
    return True


def main():
    wanted = [a.upper() for a in sys.argv[1:]] or list(ACCOUNTS)
    unknown = [w for w in wanted if w not in ACCOUNTS]
    if unknown:
        sys.exit(f"Unknown account label(s): {unknown}. Known: {list(ACCOUNTS)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            results = {w: capture_one(w, ACCOUNTS[w], context, page) for w in wanted}
        finally:
            browser.close()

    print("\n--- Summary ---")
    for label, ok in results.items():
        print(f"  {label}: {'OK' if ok else 'FAILED'}")

    if all(results.values()):
        print('\nDone. Verify with:  gh workflow run "Daily Service Button Tap"')
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
