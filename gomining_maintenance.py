import base64
import json
import math
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


def _publish_calc_json(filename, make_plan, commit_message):
    """Read `filename` from CALC_REPO, let `make_plan(current, now)` decide, write if needed.

    `make_plan` returns (new_contents, value_changed) or None for "nothing to write";
    `commit_message(new_contents, value_changed)` builds the commit message. Returns
    that (new_contents, value_changed) pair, or None if nothing was written. Retries once
    if the file changed under us (409/422); anything else raises (callers catch).
    """
    path = f"repos/{CALC_REPO}/contents/{filename}"
    for attempt in (1, 2):
        sha, current = None, None
        try:
            meta = json.loads(_calc_gh(path).stdout)
            sha = meta["sha"]
            current = json.loads(base64.b64decode(meta["content"]))
        except subprocess.CalledProcessError as exc:
            if "404" not in (exc.stderr or ""):
                raise  # not simply "file doesn't exist yet"
        plan = make_plan(current, datetime.now(timezone.utc))
        if plan is None:
            return None
        new, changed = plan
        args = [
            "-X", "PUT", path,
            "-f", f"message={commit_message(new, changed)}",
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
        return new, changed
    return None


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
        result = _publish_calc_json(
            CALC_FILE,
            lambda current, now: plan_mining_mode_file(current, percent, now),
            lambda new, changed: f"Mining mode discount: {'now' if changed else 'still'} {percent}% (nightly check)",
        )
        if result is None:
            print(f"[{label}] {CALC_FILE} already current ({percent}%, checked recently) — nothing to write.")
        else:
            print(f"[{label}] wrote {CALC_FILE}: {percent}% ({'changed' if result[1] else 'heartbeat'}).")
    except Exception as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        report(label, f"couldn't update {CALC_FILE} — {detail.strip()[:300]}", level="warning")


# ---- Lock rewards model -> the calculator's lock-model.json ----
#
# The lock page (Governance > My lock) loads two platform-wide datasets that fully
# determine GoMining's veGOMINING reward model (verified against GoMining's own Lock
# Calculator: weekly reward within 0.001% for 10K-10M GMT, every lock period):
#   * POST /api/ve-gomining-lock/statistics: totalVotes per network (sum = all lockers' votes)
#   * POST /api/mint-and-burn/index: weekly mint cycles; the `mintReward` receiver of the
#     latest cycle is the weekly reward pool shared among all votes.
# Amounts arrive as wei-style numbers (1e18 = 1 GMT).
LOCK_FILE = "lock-model.json"
LOCK_PAGE_URL = "https://app.gomining.com/lock/ve-my-lock"
STATS_PATH = "/api/ve-gomining-lock/statistics"
MINT_BURN_PATH = "/api/mint-and-burn/index"
POOL_LABEL = "mintReward"
CYCLE_WINDOW = timedelta(days=3)      # records this close to the newest one are the same weekly cycle
VOTES_RANGE = (1e6, 1e11)
POOL_RANGE = (1e3, 1e8)
YIELD_RANGE = (0.05, 1.0)             # implied yearly income per vote (0.23 today)
YIELD_TOLERANCE = 0.02                # vs the statistics' own yearlyIncomePerVote (rounded to 2 decimals)
# Rewrite only when something moved materially (votes drift a little every day; the pool
# steps at each Tuesday cycle) or when the heartbeat is due (HEARTBEAT_DAYS).
VOTES_CHANGE = 0.0025
POOL_CHANGE = 0.0005

# label -> {"total_votes", "weekly_pool_gmt", "cycle"} read during this run.
lock_models = {}


def _wei_to_gmt(value):
    """Wei-style number or numeric string -> GMT (float), or None. Rejects bool/NaN/inf/negative."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number / 1e18


def _api_rows(payload, what):
    rows = payload.get("data", {}).get("array") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{what}: no data.array rows")
    return rows


def parse_lock_model(stats_payload, mint_payload):
    """{"total_votes", "weekly_pool_gmt", "cycle"} from the two lock-page responses.

    Raises ValueError (with a public-log-safe reason) when anything is missing or implausible.
    Strict on purpose: a wrong pool or vote count silently changes every result on the calculator.
    """
    stats = _api_rows(stats_payload, "statistics")
    votes, reported = 0.0, []
    for item in stats:
        part = _wei_to_gmt(item.get("totalVotes") if isinstance(item, dict) else None)
        if part is None:
            raise ValueError("statistics: a totalVotes value is missing or not a number")
        votes += part
        rate = item.get("yearlyIncomePerVote")
        if isinstance(rate, (int, float)) and not isinstance(rate, bool):
            reported.append(rate)

    latest, cycle_rows = None, []
    for row in _api_rows(mint_payload, "mint-and-burn"):
        try:
            created = datetime.fromisoformat(str(row.get("createdAt")).replace("Z", "+00:00"))
        except ValueError:
            continue
        receivers = [r for r in (row.get("mintReceivers") or []) if isinstance(r, dict) and r.get("label") == POOL_LABEL]
        if not receivers:
            continue
        cycle_rows.append((created, receivers))
        latest = created if latest is None or created > latest else latest
    if latest is None:
        raise ValueError(f"mint-and-burn: no cycle with a '{POOL_LABEL}' receiver")
    pool = 0.0
    for created, receivers in cycle_rows:
        if latest - created > CYCLE_WINDOW:
            continue
        for receiver in receivers:
            part = _wei_to_gmt(receiver.get("value"))
            if part is None:
                raise ValueError(f"mint-and-burn: a {POOL_LABEL} value is missing or not a number")
            pool += part

    if not VOTES_RANGE[0] <= votes <= VOTES_RANGE[1]:
        raise ValueError(f"total votes {votes:.0f} outside the sane range")
    if not POOL_RANGE[0] <= pool <= POOL_RANGE[1]:
        raise ValueError(f"weekly pool {pool:.0f} outside the sane range")
    implied = 365 / 7 * pool / votes
    if not YIELD_RANGE[0] <= implied <= YIELD_RANGE[1]:
        raise ValueError(f"implied yearly income per vote {implied:.3f} outside the sane range")
    for rate in reported:
        if abs(implied - rate) > YIELD_TOLERANCE:
            raise ValueError(f"implied yearly income per vote {implied:.3f} disagrees with GoMining's own {rate}")
    return {
        "total_votes": round(votes, 2),
        "weekly_pool_gmt": round(pool, 2),
        "cycle": latest.date().isoformat(),
    }


def read_lock_model_on_fresh_page(context, label):
    """Load the lock page once and read the two datasets it fetches. Never raises.

    Records the result in lock_models. If it can't be read, logs why (a reason string, the
    response counts and /api/ URL paths; never bodies, queries or cookies).
    """
    page = None
    try:
        page = context.new_page()
        captured, api_paths = {}, []

        # A real function: Playwright can't wrap a builtin such as dict.__setitem__ as a listener.
        def on_response(response):
            path = urlsplit(response.url).path
            if path in (STATS_PATH, MINT_BURN_PATH):
                captured[path] = response
            elif path.startswith("/api/") and path not in api_paths and len(api_paths) < 25:
                api_paths.append(path)

        page.on("response", on_response)
        page.goto(LOCK_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(20):  # up to ~20s for the app to make its data requests
            if len(captured) == 2:
                break
            page.wait_for_timeout(1000)

        if len(captured) < 2:
            print(f"[{label}] lock model: not found (captured {sorted(captured)}; /api/ paths seen: {api_paths}).")
            return
        model = parse_lock_model(captured[STATS_PATH].json(), captured[MINT_BURN_PATH].json())
        lock_models[label] = model
        print(f"[{label}] Lock model read: votes={model['total_votes']:.0f} pool={model['weekly_pool_gmt']:.2f} GMT/wk "
              f"(cycle {model['cycle']}).")
    except ValueError as exc:
        print(f"[{label}] lock model: rejected — {exc}")
    except Exception as exc:
        print(f"[{label}] lock model read failed ({type(exc).__name__}); tap unaffected.")
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def plan_lock_model_file(current, model, now):
    """Return (new_contents, value_changed) for lock-model.json, or None if nothing needs writing."""
    stamp = now.strftime(_ISO_FORMAT)
    current = current if isinstance(current, dict) else {}

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0

    old_votes, old_pool = current.get("total_votes"), current.get("weekly_pool_gmt")
    moved = (
        not (num(old_votes) and num(old_pool))
        or abs(model["total_votes"] - old_votes) / old_votes > VOTES_CHANGE
        or abs(model["weekly_pool_gmt"] - old_pool) / old_pool > POOL_CHANGE
    )
    fresh = {
        "total_votes": model["total_votes"],
        "weekly_pool_gmt": model["weekly_pool_gmt"],
        "cycle": model["cycle"],
    }
    source = "GoMining app: lock statistics + latest mint cycle (nightly ServiceTap run)"
    if moved:
        return {**fresh, "changed_at": stamp, "checked_at": stamp, "source": source}, True

    try:
        checked = datetime.strptime(current.get("checked_at", ""), _ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        checked = None
    if checked is None or now - checked >= timedelta(days=HEARTBEAT_DAYS):
        return {**fresh, "changed_at": current.get("changed_at") or stamp, "checked_at": stamp, "source": source}, False
    return None


def publish_lock_model(models):
    """Keep LOCK_FILE in CALC_REPO in step with the lock model read this run. Never raises."""
    if not (CALC_REPO and CALC_REPO_TOKEN):
        return  # optional feature, not configured (e.g. a fork)
    label = "lock-model"
    try:
        if not models:
            report(label, f"couldn't read the lock model from any account; leaving {LOCK_FILE} unchanged.", level="warning")
            return
        model = next(iter(models.values()))
        result = _publish_calc_json(
            LOCK_FILE,
            lambda current, now: plan_lock_model_file(current, model, now),
            lambda new, changed: (f"Lock model: {'updated' if changed else 'still'} pool "
                                  f"{new['weekly_pool_gmt']:.0f} GMT/wk, votes {new['total_votes'] / 1e6:.1f}M (nightly check)"),
        )
        if result is None:
            print(f"[{label}] {LOCK_FILE} already current — nothing to write.")
        else:
            print(f"[{label}] wrote {LOCK_FILE}: pool {model['weekly_pool_gmt']:.2f}, votes {model['total_votes']:.0f} "
                  f"({'changed' if result[1] else 'heartbeat'}).")
    except Exception as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        report(label, f"couldn't update {LOCK_FILE} — {detail.strip()[:300]}", level="warning")


# ---- Weekly veGOMINING lock re-extension ----
#
# A lock's votes and discount decay unless its end date is pushed back out to the
# platform's max period roughly weekly ("re-maxing"). This reuses the account's
# already-authenticated session to do that for every position GoMining's own
# find-by-user call reports, skipping any position under that account's configured
# GMT threshold (so a small/dust position is left alone). Off by default: only the
# weekly lock_extend.yml workflow sets RUN_LOCK_EXTEND, so the nightly maintenance
# run never touches locks.
RUN_LOCK_EXTEND = os.environ.get("RUN_LOCK_EXTEND", "").strip().lower() in ("1", "true", "yes")
POSITIONS_PATH = "/api/ve-gomining-lock/find-by-user"
LOCK_VIEW_URL = "https://app.gomining.com/lock/ve-my-lock/{network}/view/{id}/edit?mode=date"

# Failures during this run's lock re-extension (label, short position id or None, detail) --
# folded into the run's overall pass/fail alongside the daily tap: a missed re-extension is
# a real problem (decaying votes/discount), not the purely cosmetic case the CALC_REPO
# features above are.
lock_extend_failures = []


def lock_skip_threshold_gmt(label):
    """GMT amount below which a position is left untouched for this account, or None (no skip).

    From LOCK_SKIP_THRESHOLD_<LABEL> (e.g. LOCK_SKIP_THRESHOLD_SECONDARY=50). Unset for an
    account means every position on it is re-extended.
    """
    raw = os.environ.get(f"LOCK_SKIP_THRESHOLD_{label.upper()}")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def read_positions_on_fresh_page(context, label):
    """This account's veGOMINING lock positions: [{"id","network","amount_gmt","days_to_expire"}].

    Reads the same POSITIONS_PATH call the Governance > My Lock page itself makes on load,
    rather than scraping that page's own position-list markup (unversioned Angular output,
    and not something to click blindly on a page that locks real funds). Never raises;
    returns [] if the call isn't seen or the shape is unexpected.
    """
    page = None
    try:
        page = context.new_page()
        captured = []

        def on_response(response):
            if urlsplit(response.url).path == POSITIONS_PATH:
                captured.append(response)

        page.on("response", on_response)
        page.goto(LOCK_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(20):  # up to ~20s for the app to make its data requests
            if captured:
                break
            page.wait_for_timeout(1000)

        if not captured:
            print(f"[{label}] lock positions: not found (no {POSITIONS_PATH} response).")
            return []

        rows = captured[-1].json().get("data", {}).get("array")
        if not isinstance(rows, list):
            print(f"[{label}] lock positions: unexpected response shape.")
            return []

        positions = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid, network = row.get("id"), row.get("network")
            amount = _wei_to_gmt(row.get("amountNumeric"))
            days = row.get("daysToExpire")
            valid_days = isinstance(days, (int, float)) and not isinstance(days, bool)
            if not (isinstance(pid, str) and isinstance(network, str) and amount is not None and valid_days):
                continue
            positions.append({"id": pid, "network": network, "amount_gmt": amount, "days_to_expire": days})

        summary = ", ".join(f"{p['amount_gmt']:.2f} GMT" for p in positions)
        print(f"[{label}] lock positions read: {len(positions)} ({summary}).")
        return positions
    except Exception as exc:
        print(f"[{label}] lock positions read failed ({type(exc).__name__}); lock-extend skipped this run.")
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def extend_lock_position(context, label, position):
    """Re-extend one lock position to the platform's Max period. Never raises.

    Verifies against the account's own data afterward (days_to_expire must have grown),
    not just whether the clicks completed -- a click sequence that "succeeds" but doesn't
    actually move the account's lock is exactly the silent-failure case this is meant to catch.
    """
    pid, network, before_days = position["id"], position["network"], position["days_to_expire"]
    url = LOCK_VIEW_URL.format(network=network, id=pid)
    page = None
    try:
        page = context.new_page()
        # The flow ends in a confirmation prompt; GoMining's own version of it is an
        # in-page modal (Cancel/Confirm), but accept any native dialog too (e.g. a stray
        # beforeunload) since the only action ever taken on this page is the intended one.
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        max_btn = page.get_by_role("button", name="Max", exact=True)
        try:
            max_btn.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeoutError:
            # GoMining only offers the "Max" quick-pick when there's room to extend
            # further than the lock's current end date. A lock already at (or within
            # about a week of) the platform's max period instead shows only a "Custom"
            # date picker, pre-filled with an arbitrary +1-week date -- there's nothing
            # worth re-extending to today. Same treatment as the daily tap's
            # already-on-cooldown case: nothing to do, not a failure.
            if page.get_by_role("button", name="Custom").is_visible():
                print(f"[{label}] lock {pid[:8]} ({position['amount_gmt']:.2f} GMT): "
                      f"already at (or within about a week of) the max period -- nothing to extend today.")
                return True
            raise  # neither button present -- an unrecognized page state; let the except below report it

        max_btn.click()
        page.wait_for_timeout(500)
        page.get_by_role("button", name="Next", exact=True).click(timeout=10000)
        page.wait_for_timeout(500)
        page.get_by_role("button", name="Lock", exact=True).click(timeout=10000)
        page.get_by_role("button", name="Confirm", exact=True).click(timeout=10000)
        page.wait_for_timeout(2000)

        fresh = read_positions_on_fresh_page(context, label)
        after = next((p for p in fresh if p["id"] == pid), None)
        if after is None:
            print(f"[{label}] lock {pid[:8]}: could not verify after extending (position not found on re-read).")
            return False
        if after["days_to_expire"] <= before_days + 0.1:
            print(f"[{label}] lock {pid[:8]}: days-to-expire did not increase "
                  f"({before_days:.1f} -> {after['days_to_expire']:.1f}) -- treating as failed.")
            return False

        print(f"[{label}] lock {pid[:8]} ({position['amount_gmt']:.2f} GMT): re-extended "
              f"({before_days:.1f} -> {after['days_to_expire']:.1f} days to expire).")
        return True
    except Exception as exc:
        print(f"[{label}] lock {pid[:8]} ({position['amount_gmt']:.2f} GMT): FAILED to extend "
              f"({type(exc).__name__}: {exc}).")
        if page is not None:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            try:
                page.screenshot(path=f"{DEBUG_DIR}/{label}-lock-{pid[:8]}-failure.png", full_page=True)
            except Exception:
                pass
        return False
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def extend_locks_for_account(context, label):
    """Re-extend every qualifying lock position on this account to Max.

    Best-effort per position: one position failing is reported and counted in
    lock_extend_failures, but does not stop the others or the rest of the run.
    """
    positions = read_positions_on_fresh_page(context, label)
    if not positions:
        report(label, "could not read this account's lock positions -- lock re-extend skipped this run.")
        lock_extend_failures.append((label, None, "could not read lock positions"))
        return

    threshold = lock_skip_threshold_gmt(label)
    for position in positions:
        pid_short = position["id"][:8]
        if position["amount_gmt"] <= 0:
            # An empty/closed position (0 GMT still listed by the API) -- nothing to
            # lock or extend, regardless of the account's threshold.
            print(f"[{label}] lock {pid_short}: 0 GMT locked -- nothing to extend, left alone.")
            continue
        if threshold is not None and position["amount_gmt"] < threshold:
            print(f"[{label}] lock {pid_short} ({position['amount_gmt']:.2f} GMT): "
                  f"below the {threshold:g} GMT skip threshold -- left alone.")
            continue
        if not extend_lock_position(context, label, position):
            report(label, f"could not re-extend lock {pid_short} "
                          f"({position['amount_gmt']:.2f} GMT) -- check it by hand.")
            lock_extend_failures.append((label, pid_short, f"{position['amount_gmt']:.2f} GMT"))


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
            if CALC_REPO and CALC_REPO_TOKEN and not lock_models:
                read_lock_model_on_fresh_page(context, label)
            if RUN_LOCK_EXTEND:
                extend_locks_for_account(context, label)
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
    publish_lock_model(lock_models)            # optional + best-effort; never raises

    if RUN_LOCK_EXTEND:
        if lock_extend_failures:
            print(f"\n--- Lock re-extend: {len(lock_extend_failures)} failure(s) ---")
            for fail_label, pid, detail in lock_extend_failures:
                print(f"{fail_label}: {detail}" + (f" (lock {pid})" if pid else ""))
        else:
            print("\nLock re-extend: OK")

    overall_ok = all(results.values()) and not lock_extend_failures

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
