# gomining-servicetap

Automates tapping GoMining's daily "maintenance" button for one or more accounts (configured via `GOMINING_ACCOUNT_LABELS`) so their maintenance-discount streak never lapses, without needing a laptop to be on. Runs on GitHub Actions. See `README.md` for user-facing setup instructions; this file is operational/maintainer notes.

## How it works

- `gomining_maintenance.py`: for each configured account, loads that account's saved session cookies into a headless Playwright browser, opens the dashboard, and clicks the maintenance button (selector: `button:has(icon-broom)`, a broom icon that's GoMining's own icon for this button) if it's not already on cooldown.
- No passwords are ever stored. Auth is via session cookies captured through a one-time live login — run `recapture.py`, or use the `capture-cookies` skill.
- After every successful run, the script re-saves that account's current cookies back to its GitHub secret (`persist_refreshed_cookies()`). **This is required, not optional**: GoMining rotates its `refresh_token` on use, so a static cookie saved once will work exactly once and then permanently fail.
- `REPO` (used for the self-refresh `gh secret set` call) and the account list are both derived at runtime, not hardcoded; see `GITHUB_REPOSITORY` (set automatically by GitHub Actions) and `GOMINING_ACCOUNT_LABELS` in the workflow env. This is what makes the repo fork-portable.
- Each account gets up to `MAX_ATTEMPTS` (3) tries within a single run, `RETRY_DELAY_SECONDS` (10) apart, before being reported as failed (added 2026-08-20, see "Retry logic" below). The one exception is a detected dead session (see "Session-expiry detection" below): that's returned immediately without retrying, since a rejected session fails identically every time and retrying it just burns ~90 seconds for nothing.

## Page load & readiness — do NOT use `networkidle` (fixed 2026-09-03)

`page.goto()` uses `wait_until="domcontentloaded"` (HTML parsed), deliberately **not** `"networkidle"`. The GoMining dashboard is a live app that holds websocket/polling connections open for mining stats, so the network never goes quiet for the 500ms `"networkidle"` requires — `page.goto` then times out at 30s and the whole attempt fails **even though the page loaded fine**. This caused the 2026-09-03 night failure (PRIMARY struck out all 3 retries with `Page.goto: Timeout 30000ms exceeded ... waiting until "networkidle"`; the debug screenshot showed the page fully rendered with the button already on cooldown). Playwright's own docs also explicitly discourage `"networkidle"`.

Readiness is instead confirmed by explicit waits after the navigation:
- The cookie-consent modal (`<consent-popup>`, "Accept necessary" / "Accept all" buttons) is dismissed first if present — its full-screen overlay can otherwise intercept the button click. Best-effort, wrapped in `try/except`, never fails the run.
- `button.wait_for(state="visible", timeout=30000)` — a real "dashboard rendered" signal, stronger than the old `state="attached"`. On timeout it distinguishes a dead session from a genuine load failure (see "Session-expiry detection" below).
- A `page.wait_for_timeout(1500)` settle so the button's cooldown/disabled state has loaded from the API before it's read (guards a race where it briefly renders enabled on empty state).

## Session-expiry detection (fixed 2026-09-06)

**GoMining changed two things on their side around 2026-09-06, both of which broke the automation the same night (both accounts, ~18h after a good run):**

1. **The auth cookie set shrank from 9 names to 3.** It's now just `access_token` (a ~1h JWT), `refresh_token` (long-lived, rotates on use), and `cf_clearance`. The old `brwsr` / `irtps` / `sa-user-id*` / `viewport` cookies are gone. That change invalidated every existing saved session at once and forced a full re-capture. `KEEP_COOKIE_NAMES` in `gomining_maintenance.py` (and the same list in `recapture.py` and the `capture-cookies` skill) was trimmed to match — keep all three in sync.
2. **A logged-out visitor is no longer redirected to `/login`.** GoMining now renders a "guest" stub of the miners page (`<h1 class="nft-page-stub__guest-title">Grow your mining farm</h1>`) at the same `/nft-miners` URL. The old `if "/login" in page.url` check never fired, so a dead session degraded into a full ~5-minute retry grind (3 attempts × 2 accounts) ending in a vague failure email.

**Detection now:** `button.wait_for(state="visible", timeout=30000)` as before, but wrapped so that on `TimeoutError` it checks whether `.nft-page-stub__guest-title` is visible (or the URL still contains `/login`). If so → `report(label, "session expired -- ...")` and `return False` immediately, no retry. If not → `raise`, and the normal retry loop handles it as a genuine load failure. **Do not check for the guest stub before the button times out:** that same stub flashes for a second or two on every *normal* logged-in load while the session validates, so an instant check false-positives (learned the hard way — a first attempt at this shipped a racy `button.or_(signed_out)` check and every run reported "session expired" even with perfectly good cookies). Cost of the corrected version: a real dead session takes ~30s per account (one button-wait timeout) instead of the old ~2 min, still far better than the pre-fix 5 min.

`report()` sends the message to Sentry at `level="error"`, which Sentry files as a high-priority issue — and the project's default alert rule ("Send a notification for high priority issues", emails active members) turns that into an email. So a "session expired" run notifies without any bespoke alert config; just make sure Sentry email notifications are on for your account.

**Recovery:** `python recapture.py` (both accounts) or `python recapture.py <LABEL>` (one). It opens a headed browser, the owner logs in via "Continue with Google", and it pushes the fresh cookies straight to the `GOMINING_COOKIES_<LABEL>` secret. The `capture-cookies` skill is the Claude-driven equivalent for when the user can't run the script themselves.

**2FA and this automation:** enabling 2FA (on the GoMining account or the Google account it signs in with) does **not** affect the nightly run on an ongoing basis — the run reuses a saved session and never hits a login form, so no 2FA prompt is ever reached. It will, however, invalidate the current session *once* when first turned on, producing a single "session expired" run; `recapture.py` fixes it and subsequent runs are normal. The only thing 2FA genuinely rules out is *fully scripting* the re-login step, which is manual by design anyway.

## The reset mechanic (important, learned the hard way)

The maintenance discount resets on a **fixed UTC calendar-day boundary (00:00 UTC)**, not a rolling 24h cooldown from your last click (confirmed via GoMining's own FAQ: https://help.nft.gomining.com/faq/maintenance-fees-and-discounts). The countdown timer shown in the UI is always counting down to the *same* daily reset point, not to "24h after you clicked." **Missing an entire UTC day resets the whole accumulated discount streak to zero**, not just that day's increment, so reliability matters more than it might first appear.

## GitHub Actions scheduling gotchas

1. **The `on.schedule` cron can get "stuck."** GitHub's workflow registration doesn't always pick up edits to the schedule on a normal push. Symptom: you push a new cron, but runs keep firing (or not firing) on the old schedule, and `gh api repos/OWNER/REPO/actions/workflows/ID` shows `updated_at` frozen at the original creation time despite multiple pushes. **Fix: after ANY change to `on.schedule`, run (from inside the repo directory; `gh` infers the repo from the git remote, no `--repo` flag needed):**
   ```
   gh workflow disable "Daily Service Button Tap"
   gh workflow enable "Daily Service Button Tap"
   ```
   Confirm it worked by checking `updated_at` moved to just now (`gh api repos/{owner}/{repo}/actions/workflows/{workflow_id} --jq '{state, updated_at}'`; get the workflow ID from `gh workflow list`).
2. **GitHub's scheduler is best-effort.** Expect 15–45 minute delays past the target time, and occasionally a dropped slot entirely, this is documented GitHub behavior (schedule events can be delayed or dropped under load, especially at `:00`), not a bug here. This is why the schedule runs multiple attempts rather than a single exact time.
3. **The first day (or the first day after any schedule edit) tends to be the flakiest.** Our very first-ever scheduled workflow on this repo took >24h to fire even once. Every time we've since edited the schedule, cron, or done a disable/enable cycle, the following several hours have shown more misses than the days before/after. Not proven to be causal (could just be small-sample noise), but the pattern repeated enough times this session to be worth expecting rather than panicking over. Give a fresh schedule real, untouched time before concluding something's actually broken.
4. **Don't judge reliability from short-notice test crons.** Pushing a one-off cron just 2-5 minutes ahead and watching for it repeatedly gave inconsistent results even right after a fresh disable/enable, likely because propagation itself takes real time, not just the registration action. Test by checking a full day's worth of real runs (`gh api repos/{owner}/{repo}/actions/runs`) instead of a quick manual probe.
5. Current schedule lives in `.github/workflows/maintenance.yml`; it's been iterated on a lot; check `git log` for the reasoning before changing it again. As of 2026-08-20 it's `15 23,0-5 * * *` (7 attempts, 7:15pm-1:15am ET), widened from 6 attempts after noticing the first slot of the night was going missing on several recent nights; the extra hour-early attempt is a buffer against that, not a fix for it (GitHub's scheduler being best-effort is still the underlying cause, see point 2 above).

## Retry logic (added 2026-08-20)

A real incident exposed a gap: one account hit an ordinary page-load timeout (`Page.goto`/`Locator.wait_for` exceeding 30s, nothing to do with the saved session's validity) on the *last* scheduled attempt of the night. Because a failed run never refreshes that account's cookies, and the next scheduled attempt was ~20 hours away (the following night's window), those cookies just sat idle far longer than they normally do between refreshes, and by the next attempt the session had genuinely gone stale (redirected to `/login` for real). One transient, unrelated-to-auth hiccup snowballed into a real dead session purely because of *when* it happened to occur.

Fix: `run_for_account()` now retries the navigate-and-click sequence up to `MAX_ATTEMPTS` (3) times in-process, `RETRY_DELAY_SECONDS` (10s) apart, before giving up. This catches short-lived hiccups (slow page load, flaky network) within the same run instead of losing an entire day to them. It deliberately does *not* apply to a detected dead session (see "Session-expiry detection") — that's real, not a fluke, and retrying it 3 times just wastes ~90 seconds confirming what we already know.

Knock-on effects of this change, all already applied:
- Job-level `timeout-minutes` in `maintenance.yml` bumped 3 → 6 to cover the new worst-case (2 accounts, both retrying the full 3 attempts).
- Sentry's `max_runtime` in `main()` bumped 5 → 8 to match, so Sentry doesn't flag a legitimately-still-retrying run as stuck.
- A `FAILED` result (and the resulting GitHub email) now means an account struck out 3 times, not once, this is a good thing (fewer false-alarm emails for things that self-resolve) but means a "FAILED" email is a stronger signal that something's actually wrong.

## GitHub CLI multi-account gotcha

If you have more than one `gh` login on your machine and `git push` / `gh` commands fail with "Repository not found" on a repo you can see exists, that's the tell: GitHub returns that error (not "forbidden") when the *active* account can't see a private repo, rather than confirming its existence to an unauthorized account. Check `gh auth status` for which account is active, and run `gh auth switch` / `gh auth setup-git` as needed (the latter because Windows' Git Credential Manager doesn't automatically follow `gh auth switch`). Pushing to `.github/workflows/` also specifically requires the `workflow` OAuth scope (`gh auth refresh -h github.com -s workflow` if a push gets rejected for missing scope).

## Secrets in this repo

- `GOMINING_COOKIES_<LABEL>` (one per account in `GOMINING_ACCOUNT_LABELS`): session cookies (JSON array), self-refreshed by the script every run. Run `recapture.py` (or the `capture-cookies` skill) to re-capture from scratch after a "session expired".
- `GH_PAT_SECRETS_WRITE`: fine-grained PAT scoped to only this repo, Secrets: read/write, nothing else. Used by the script to call `gh secret set` and self-refresh the cookie secrets above.
- `SENTRY_DSN`: optional. Script and workflow both run fine without it (guarded by `if SENTRY_DSN:` throughout); just no Sentry visibility if unset.

## Timeouts (added after a real 49-minute hang, 2026-08-17)

The "Install dependencies" step (`pip install` + `playwright install --with-deps chromium`) hung for 49+ minutes one night, most likely a transient GitHub-runner/network hiccup, not anything in this repo's code (see git log for the incident). Two timeouts now guard against a repeat, both in `.github/workflows/maintenance.yml`:
- Job-level `timeout-minutes: 6` (bumped from 3 on 2026-08-20 to accommodate the retry logic below, see "Retry logic" section): every observed successful run still completes in under a minute, this headroom exists for the worst case where multiple accounts each burn through all 3 retry attempts. A timeout-killed run counts as *failed*, which triggers GitHub's failure email; before this existed, a hang like that was invisible.
- Step-level `timeout-minutes: 2` on "Install dependencies" specifically: isolates *which* step hung in the Actions log, instead of a generic job-level timeout with no clue where.

If you tighten these further, check actual observed run durations first (`gh run list` shows durations) rather than guessing.

## Sentry (error monitoring + cron check-ins)

Optional, wired in `gomining_maintenance.py` guarded by `if SENTRY_DSN:`. Two things worth knowing if you touch this:

- **`include_local_variables=False` in `sentry_sdk.init()` is deliberate, not an oversight.** Sentry's default captures local variable *values* in stack traces. This script holds live session cookies in local variables (`cookies`, `cookies_json`); leaving the default on would leak bearer credentials into Sentry on any exception. Don't turn this back on without solving that first.
- **Crons check-in uses manual `capture_checkin()`, not the `@monitor` decorator.** The decorator only catches `Exception`; this script signals failure via `sys.exit(1)`, which raises `SystemExit` (a `BaseException`, not caught by a bare `except Exception`). The decorator would have silently reported `OK` on a real failure. Manual check-ins report success/failure based on the script's own `all(results.values())` check instead.
- **The monitor's cron schedule (`15 23,0-5 * * *`, UTC) is hardcoded in `main()` and must be kept in sync with `.github/workflows/maintenance.yml`'s `on.schedule` by hand**: they're two separate config values that happen to need the same value, not one shared source of truth.
- **Known gap**: Sentry only starts once the Python script runs; a hang in "Install dependencies" (like the 2026-08-17 incident above) happens *before* that, so it produces no Sentry error, only an eventual server-side MISSED alert once `checkin_margin` (60 min) elapses. The step-level timeout above is the actual mitigation for that specific failure mode, not Sentry.
- Org: `gomining-service-button`, a dedicated Sentry org, kept separate from other unrelated projects on the same Sentry login (one login, multiple orgs, no reconnection needed to switch between them).

## Releases — cut one after every substantive merge

**After any substantive change merges to `main`** (a fix, a new capability — anything a forker would want), **cut the next release before considering the work done.** No formal semver; just sequential `vN` checkpoints. From an up-to-date `main`:

```
git tag -a vN -m "vN — <one-line summary>" <merge-commit-sha>
git push origin vN
gh release create vN --verify-tag --title "vN — <short title>" --notes "<bulleted what-changed-and-why>"
```

Match the notes style of the existing releases (run `gh release view` on the latest to see it): a short intro line, then plain bullets aimed at someone deciding whether to pull it.

Why it's not optional: forks have no other update signal. `README.md`'s "Staying up to date" section points forkers at **Watch → Releases**, and nobody watches raw commits — a merge with no release is invisible to them. The releases double as the project's only changelog. `gh release list` shows the history.

## If a scheduled run fails

GitHub emails on failure, and (if `SENTRY_DSN` is set) so does Sentry's default high-priority-issue alert rule — so a "session expired" shouldn't sit unnoticed as long as Sentry email notifications are on for your account. Check the run log first: a `[LABEL] FAILED: session expired` line means that account's saved session is dead — run `python recapture.py <LABEL>` (or use the `capture-cookies` skill). GoMining invalidates sessions on their side periodically, so this is expected occasionally, not a bug. Failures from any *other* cause also upload a screenshot + HTML snapshot as a workflow artifact (`debug-artifacts`); a "session expired" return does not (it's self-explanatory, and skips straight past the artifact-saving path).
