import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import sentry_sdk
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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

# Session cookies to persist when saving a session; everything else
# (marketing/analytics -- utm_*, _ga, ajs_*, intercom-*, posthog, etc.)
# is auth-irrelevant noise. As of 2026-09-06 GoMining's login sets just
# these three. The older set (brwsr, irtps, sa-user-id*, viewport) was
# dropped on their side, and that change invalidated every existing
# session at once -- forcing a full re-capture (see the capture-cookies
# skill). Note
# access_token is a ~1h JWT; refresh_token is long-lived but rotates on
# use, which is why every successful run re-saves the live cookies (see
# persist_refreshed_cookies).
KEEP_COOKIE_NAMES = ["access_token", "refresh_token", "cf_clearance"]

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

# ---- Optional: publish the Mining mode discount to a public calculator ----
#
# The dashboard fetches /api/user/get-my-nft-discount on every load. Its
# `rewardDistributionDiscount` field is the platform-wide Mining mode discount
# (set weekly by the veGOMINING vote), sent as a FRACTION: 0.0135 == 1.35%.
# We read it from that response instead of scraping the page, then keep
# mining-mode.json in CALC_REPO up to date. Entirely optional and fork-safe:
# with CALC_REPO / CALC_REPO_TOKEN unset (as in any fork) none of it runs.
#
# CALC_REPO_TOKEN is a separate fine-grained PAT (Contents: read/write on the
# calculator repo only). It is passed to `gh` per call and never touches the
# GH_TOKEN used for the cookie secrets.
CALC_REPO = os.environ.get("CALC_REPO")
CALC_REPO_TOKEN = os.environ.get("CALC_REPO_TOKEN")
CALC_FILE = "mining-mode.json"
DISCOUNT_API_PATH = "/api/user/get-my-nft-discount"
DISCOUNT_FIELD = "rewardDistributionDiscount"
MAX_DISCOUNT_FRACTION = 0.10  # sanity ceiling (10%); anything above is treated as unreadable
# Re-stamp checked_at at least this often even when the value is unchanged, so
# the calculator's "verified within 7 days" warning means "the job stopped",
# not "the vote hasn't changed". Well under the calculator's 7-day threshold.
HEARTBEAT_DAYS = 2
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# label -> discount percent read during this run (filled by run_for_account).
mining_discounts = {}


def persist_refreshed_cookies(label, env_var, context):
    """Save this account's current cookies back to its GitHub secret.

    The site appears to rotate its refresh token on use — the first
    time a saved session is used to silently renew, the old refresh
    token stops working. Called whenever the session was confirmed
    authenticated during this run, not only on a fully successful
    tap: the rotation happens on page load (see `authenticated` in
    run_for_account), so a run that authenticates fine but then fails
    for an unrelated reason (the click never registers, a later retry
    hits a load error) would otherwise strand the newly-rotated
    cookies in memory and leave the stored secret one rotation behind
    — dead the next time it's used.
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


def _find_field(node, name, depth=0):
    """First value stored under key `name`, searching nested dicts/lists (bounded).

    The API wraps its data (the field is not at the top level of the JSON), so a
    top-level lookup finds nothing.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        if name in node:
            return node[name]
        children = node.values()
    elif isinstance(node, list):
        children = node[:50]
    else:
        return None
    for child in children:
        found = _find_field(child, name, depth + 1)
        if found is not None:
            return found
    return None


def parse_mining_discount(payload):
    """Mining mode discount as a percent (1.35) from the discount API payload.

    Returns None unless the field is a real number within [0, MAX_DISCOUNT_FRACTION].
    Strict on purpose: a bool, string or null must never be coerced into a
    discount (float(None)/float("") style coercions would publish a wrong 0%).
    """
    value = _find_field(payload, DISCOUNT_FIELD)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= MAX_DISCOUNT_FRACTION:
        return None
    return round(value * 100, 4)


def read_mining_discount(captured, label):
    """Read the discount from the response(s) the dashboard already fetched. Never raises."""
    for response in reversed(captured):
        try:
            payload = response.json()
            percent = parse_mining_discount(payload)
        except Exception as exc:
            print(f"[{label}] mining discount: couldn't read a response ({type(exc).__name__}).")
            continue
        if percent is not None:
            return percent
        # Key NAMES only (never values): shows whether the shape changed, and stays
        # safe in a public Actions log.
        shape = sorted(payload)[:20] if isinstance(payload, dict) else type(payload).__name__
        print(f"[{label}] mining discount: response had no usable '{DISCOUNT_FIELD}' "
              f"(top-level keys: {shape}).")
    return None


def read_discount_on_fresh_page(context, label):
    """Load the dashboard once more on a fresh page and read the discount it fetches.

    This is the method the discovery probe proved live. (Reading it off the tap's
    own first page load was tried and found nothing: that load doesn't reliably
    request this data, while a second load in the same session does.) Records the
    result in mining_discounts. Best-effort: never raises, never touches the tap.

    If nothing is found it logs the /api/ URL *paths* seen (no queries, bodies or
    cookies; Actions logs on a public repo are world-readable) so the next look
    doesn't need another probe.
    """
    page = None
    try:
        page = context.new_page()
        captured, api_paths = [], []

        # A real function: Playwright can't wrap a builtin such as list.append
        # as an event listener (AttributeError: ... '_pw_impl_instance_').
        def on_response(response):
            path = urlsplit(response.url).path
            if path == DISCOUNT_API_PATH:
                captured.append(response)
            elif path.startswith("/api/") and path not in api_paths and len(api_paths) < 25:
                api_paths.append(path)

        page.on("response", on_response)
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(10):  # up to ~10s for the app to make its data requests
            if captured:
                break
            page.wait_for_timeout(1000)

        percent = read_mining_discount(captured, label)
        if percent is None:
            print(f"[{label}] mining discount: not found ({len(captured)} matching response(s); "
                  f"/api/ paths seen: {api_paths}).")
        else:
            mining_discounts[label] = percent
            print(f"[{label}] Mining mode discount read: {percent}%")
    except Exception as exc:
        print(f"[{label}] mining discount read failed ({type(exc).__name__}); tap unaffected.")
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def plan_mining_mode_file(current, percent, now):
    """Return (new_contents, value_changed) for mining-mode.json, or None if nothing needs writing.

    Writes when the value changed (stamping changed_at), or when the last
    check is older than HEARTBEAT_DAYS (stamping only checked_at).
    """
    stamp = now.strftime(_ISO_FORMAT)
    current = current if isinstance(current, dict) else {}
    old = current.get("value")
    unchanged = (
        isinstance(old, (int, float)) and not isinstance(old, bool)
        and abs(old - percent) < 1e-9
    )
    source = "GoMining app (nightly ServiceTap run)"

    if not unchanged:
        return {"value": percent, "changed_at": stamp, "checked_at": stamp, "source": source}, True

    try:
        checked = datetime.strptime(current.get("checked_at", ""), _ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        checked = None
    if checked is None or now - checked >= timedelta(days=HEARTBEAT_DAYS):
        return {
            "value": percent,
            "changed_at": current.get("changed_at") or stamp,
            "checked_at": stamp,
            "source": source,
        }, False
    return None


def _calc_gh(*args):
    """`gh api ...` against the calculator repo, authenticated with CALC_REPO_TOKEN only."""
    return subprocess.run(
        ["gh", "api", *args],
        check=True, capture_output=True, text=True, timeout=30,
        env={**os.environ, "GH_TOKEN": CALC_REPO_TOKEN},
    )


def publish_mining_discount(discounts):
    """Keep CALC_FILE in CALC_REPO in step with the discount read this run.

    Best-effort by design: it never raises and never affects the run's result.
    The daily tap is what matters; if this fails, the calculator's own
    "not verified in 7 days" warning is the backstop.
    """
    if not (CALC_REPO and CALC_REPO_TOKEN):
        return  # optional feature, not configured (e.g. a fork)
    label = "mining-discount"
    try:
        if not discounts:
            report(label, "couldn't read the Mining mode discount from any account; "
                          f"leaving {CALC_FILE} unchanged.", level="warning")
            return
        percent = max(discounts.values())  # platform-wide; an account in another mode could read lower
        if len(set(discounts.values())) > 1:
            print(f"[{label}] accounts read different values ({discounts}); using the highest.")

        path = f"repos/{CALC_REPO}/contents/{CALC_FILE}"
        for attempt in (1, 2):  # second pass only if the file changed under us (sha conflict)
            sha, current = None, None
            try:
                meta = json.loads(_calc_gh(path).stdout)
                sha = meta["sha"]
                current = json.loads(base64.b64decode(meta["content"]))
            except subprocess.CalledProcessError as exc:
                if "404" not in (exc.stderr or ""):
                    raise  # not simply "file doesn't exist yet"
            plan = plan_mining_mode_file(current, percent, datetime.now(timezone.utc))
            if plan is None:
                print(f"[{label}] {CALC_FILE} already current ({percent}%, checked recently) — nothing to write.")
                return
            new, changed = plan
            args = [
                "-X", "PUT", path,
                "-f", f"message=Mining mode discount: {'now' if changed else 'still'} {percent}% (nightly check)",
                "-f", "content=" + base64.b64encode((json.dumps(new, indent=2) + "\n").encode()).decode(),
            ]
            if sha:
                args += ["-f", f"sha={sha}"]
            try:
                _calc_gh(*args)
            except subprocess.CalledProcessError as exc:
                if attempt == 1 and any(code in (exc.stderr or "") for code in ("409", "422")):
                    continue
                raise
            print(f"[{label}] wrote {CALC_FILE}: {percent}% ({'changed' if changed else 'heartbeat'}).")
            return
    except Exception as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        report(label, f"couldn't update {CALC_FILE} — {detail.strip()[:300]}", level="warning")


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
    # True as soon as the dashboard renders for a real logged-in session
    # (not the guest stub) — the earliest point the cookie rotation from
    # persist_refreshed_cookies is safe to save, independent of whether
    # the button click itself goes on to succeed.
    authenticated = False

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            page = context.new_page()
            try:
                # "domcontentloaded" (HTML parsed), NOT "networkidle": the
                # dashboard is a live app that keeps websockets/polling open
                # for mining stats, so the network never stays idle for the
                # 500ms "networkidle" wants and page.goto times out even
                # though the page loaded fine. Readiness is confirmed by the
                # explicit element waits below instead. (Playwright's own
                # docs discourage "networkidle" for exactly this reason.)
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)

                # The cookie-consent modal's full-screen overlay can sit on
                # top of the maintenance button and swallow the click. Dismiss
                # it if present (a fresh browser context each run means it
                # usually shows). Best-effort -- never fail the run over it.
                try:
                    consent = page.get_by_role(
                        "button", name="Accept necessary"
                    ).or_(page.get_by_role("button", name="Accept all"))
                    consent.first.click(timeout=5000)
                    print(f"[{label}] dismissed the cookie-consent banner.")
                except Exception:
                    pass

                # Wait for the maintenance button. "visible" (not just
                # "attached") is the real "dashboard has rendered" signal,
                # since we don't wait on networkidle.
                #
                # If it never comes, work out *why* before deciding to
                # retry: GoMining no longer redirects a logged-out visitor
                # to /login -- it renders a "guest" stub of this page
                # (`.nft-page-stub__guest-title`, "Grow your mining farm")
                # at the same URL. That stub ALSO flashes briefly on a
                # normal logged-in load before the session validates, which
                # is why we only check for it *after* the button wait times
                # out -- by then a real dashboard would have rendered.
                button = page.locator(BUTTON_SELECTOR).first
                try:
                    button.wait_for(state="visible", timeout=30000)
                except PlaywrightTimeoutError:
                    guest_stub = page.locator(".nft-page-stub__guest-title")
                    if guest_stub.is_visible() or "/login" in page.url:
                        # Dead session -- fails identically on every retry,
                        # so return now instead of burning the rest.
                        # report() mirrors this to Sentry, where an alert
                        # rule turns it into an email.
                        report(label, "session expired -- GoMining served its guest / "
                                      "signup page instead of the dashboard. Re-capture "
                                      "cookies (capture-cookies skill).")
                        return False
                    raise  # genuine load failure -- let the retry loop handle it

                # Confirmed authenticated (real dashboard, not the guest
                # stub) -- safe to persist cookies in `finally` even if
                # everything past this point still ends up failing.
                authenticated = True

                # Brief settle so the button's cooldown/disabled state has
                # loaded from the API before we read it below (avoids a race
                # where it briefly renders enabled on stale/empty state).
                page.wait_for_timeout(1500)

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
        if authenticated:
            # After the tap, before the cookie save (so the saved cookies are the
            # latest rotation). Best-effort and self-contained: it can't affect
            # the tap's result or skip the save.
            if CALC_REPO and CALC_REPO_TOKEN and not mining_discounts:
                read_discount_on_fresh_page(context, label)
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

    publish_mining_discount(mining_discounts)  # optional + best-effort; never raises

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
