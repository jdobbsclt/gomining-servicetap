import json
import os
import subprocess
import sys
import time

import sentry_sdk
from playwright.sync_api import sync_playwright
from sentry_sdk.crons import capture_checkin
from sentry_sdk.crons.consts import MonitorStatus

DASHBOARD_URL = "https://app.gomining.com/nft-miners"
BUTTON_SELECTOR = "button:has(icon-broom)"
DEBUG_DIR = "debug-artifacts"
MONITOR_SLUG = "gomining-daily-maintenance"

# A single scheduled run may hit a one-off page-load hiccup unrelated to
# whether the saved session is actually valid. Retrying a couple of times
# in-process catches those without waiting for the next scheduled run,
# which on the last attempt of the night could otherwise be ~20 hours away.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10

# GitHub Actions sets this automatically for every run -- no need to
# hardcode a repo path, which keeps this script portable to any fork.
REPO = os.environ.get("GITHUB_REPOSITORY")

SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment="production",
        traces_sample_rate=1.0,
        # Local variables in this script include live session cookies
        # (bearer credentials). Sentry's default captures local variable
        # *values* in stack traces -- leaving that on would leak them into
        # Sentry on any exception. Deliberately off, not an oversight.
        include_local_variables=False,
    )

# Cookie names worth keeping when saving a session. Excludes marketing/
# analytics cookies (utm_*, _ga, _fbp, hotjar, etc.) that don't matter
# for auth and just add noise.
KEEP_COOKIE_NAMES = [
    "cf_clearance", "brwsr", "irtps", "access_token", "refresh_token",
    "sa-user-id", "sa-user-id-v2", "sa-user-id-v3", "viewport",
]

# Which accounts to run, driven by GOMINING_ACCOUNT_LABELS (comma-separated,
# e.g. "PRIMARY,SECONDARY") so forks can run 1 or N accounts without touching this
# file -- just set that variable and add a matching GOMINING_COOKIES_<LABEL>
# secret for each label in .github/workflows/maintenance.yml.
ACCOUNT_LABELS = [
    label.strip() for label in os.environ.get("GOMINING_ACCOUNT_LABELS", "").split(",")
    if label.strip()
]
ACCOUNTS = [
    {"label": label, "env_var": f"GOMINING_COOKIES_{label.upper()}"}
    for label in ACCOUNT_LABELS
]


def persist_refreshed_cookies(label, env_var, context):
    """Save this account's current cookies back to its GitHub secret.

    The site appears to rotate its refresh token on use — the first
    time a saved session is used to silently renew, the old refresh
    token stops working. Re-saving the live cookies after every
    successful run keeps the stored secret from ever going stale.
    """
    if not os.environ.get("GH_TOKEN"):
        print(f"[{label}] skipping secret refresh — no GH_TOKEN available (expected when testing locally).")
        return

    if not REPO:
        print(f"[{label}] skipping secret refresh — GITHUB_REPOSITORY not set (expected when testing locally).")
        return

    cookies = context.cookies()
    relevant = [c for c in cookies if c["domain"].endswith("gomining.com") and c["name"] in KEEP_COOKIE_NAMES]
    cookies_json = json.dumps(relevant)

    try:
        subprocess.run(
            ["gh", "secret", "set", env_var, "--repo", REPO, "--body", cookies_json],
            check=True, capture_output=True, text=True, timeout=30,
        )
        print(f"[{label}] refreshed {env_var} with the current session cookies.")
    except subprocess.CalledProcessError as exc:
        print(f"[{label}] WARNING: could not refresh {env_var} — {exc.stderr.strip()}")
    except Exception as exc:
        print(f"[{label}] WARNING: could not refresh {env_var} — {exc}")


def report(label, message, level="error"):
    """Print for the GitHub Actions log, and mirror to Sentry if configured."""
    print(f"[{label}] {'FAILED' if level == 'error' else 'WARNING'}: {message}")
    if SENTRY_DSN:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("account", label)
            sentry_sdk.capture_message(f"[{label}] {message}", level=level)


def run_for_account(playwright, label, env_var, cookies_json):
    cookies = json.loads(cookies_json)

    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    )
    context.add_cookies(cookies)
    success = False

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            page = context.new_page()
            try:
                page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30000)

                if "/login" in page.url:
                    # A dead session will fail the same way every time --
                    # retrying wastes time instead of catching a fluke.
                    report(label, "redirected to login — saved session has expired.")
                    return False

                button = page.locator(BUTTON_SELECTOR).first
                button.wait_for(state="attached", timeout=30000)

                if button.get_attribute("disabled") is not None:
                    print(f"[{label}] OK: maintenance button already on cooldown — nothing to do.")
                    success = True
                    return True

                button.click()
                page.wait_for_timeout(2000)

                if button.get_attribute("disabled") is not None:
                    print(f"[{label}] OK: clicked maintenance button successfully.")
                    success = True
                    return True

                report(label, "clicked the button but it never went into cooldown — unclear if it worked.")
                return False

            except Exception as exc:
                if attempt < MAX_ATTEMPTS:
                    print(f"[{label}] attempt {attempt}/{MAX_ATTEMPTS} hit a transient error ({exc}), retrying in {RETRY_DELAY_SECONDS}s...")
                    page.close()
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue

                print(f"[{label}] FAILED: unexpected error after {MAX_ATTEMPTS} attempts — {exc}")
                print(f"[{label}] page url at time of failure: {page.url}")
                if SENTRY_DSN:
                    with sentry_sdk.new_scope() as scope:
                        scope.set_tag("account", label)
                        sentry_sdk.capture_exception(exc)
                os.makedirs(DEBUG_DIR, exist_ok=True)
                try:
                    page.screenshot(path=f"{DEBUG_DIR}/{label}-failure.png", full_page=True)
                    with open(f"{DEBUG_DIR}/{label}-failure.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                    print(f"[{label}] saved debug screenshot + HTML to {DEBUG_DIR}/")
                except Exception as debug_exc:
                    print(f"[{label}] could not save debug artifacts: {debug_exc}")
                return False

    finally:
        if success:
            persist_refreshed_cookies(label, env_var, context)
        context.close()
        browser.close()


def main():
    check_in_id = None
    if SENTRY_DSN:
        check_in_id = capture_checkin(
            monitor_slug=MONITOR_SLUG,
            status=MonitorStatus.IN_PROGRESS,
            monitor_config={
                # Mirrors .github/workflows/maintenance.yml's cron exactly.
                "schedule": {"type": "crontab", "value": "15 23,0-5 * * *"},
                "timezone": "UTC",
                # GitHub's scheduler is best-effort and we've directly
                # observed 45+ min delays -- this must stay looser than
                # that or Sentry will cry wolf on normal lag.
                "checkin_margin": 60,
                # Retries can now push a real run close to the job-level
                # timeout (see maintenance.yml); keep headroom above that.
                "max_runtime": 8,
                "failure_issue_threshold": 1,
                "recovery_threshold": 1,
            },
        )

    if not ACCOUNTS:
        msg = "no accounts configured. Set GOMINING_ACCOUNT_LABELS (comma-separated, e.g. \"PRIMARY,SECONDARY\") in the workflow env."
        print(f"FAILED: {msg}")
        if SENTRY_DSN:
            sentry_sdk.capture_message(msg, level="error")
            capture_checkin(monitor_slug=MONITOR_SLUG, check_in_id=check_in_id, status=MonitorStatus.ERROR)
            sentry_sdk.flush()
        sys.exit(1)

    results = {}

    with sync_playwright() as playwright:
        for account in ACCOUNTS:
            cookies_json = os.environ.get(account["env_var"])
            if not cookies_json:
                report(account["label"], f"no cookies found in {account['env_var']}.")
                results[account["label"]] = False
                continue

            results[account["label"]] = run_for_account(
                playwright, account["label"], account["env_var"], cookies_json
            )

    print("\n--- Summary ---")
    for label, ok in results.items():
        print(f"{label}: {'OK' if ok else 'FAILED'}")

    overall_ok = all(results.values())

    if SENTRY_DSN:
        capture_checkin(
            monitor_slug=MONITOR_SLUG,
            check_in_id=check_in_id,
            status=MonitorStatus.OK if overall_ok else MonitorStatus.ERROR,
        )
        sentry_sdk.flush()

    if not overall_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
