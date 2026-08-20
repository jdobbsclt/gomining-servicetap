# gomining-servicetap

Automates tapping GoMining's daily "maintenance" button for one or more accounts (configured via `GOMINING_ACCOUNT_LABELS`) so their maintenance-discount streak never lapses, without needing a laptop to be on. Runs on GitHub Actions. See `README.md` for user-facing setup instructions; this file is operational/maintainer notes.

## How it works

- `gomining_maintenance.py`: for each configured account, loads that account's saved session cookies into a headless Playwright browser, opens the dashboard, and clicks the maintenance button (selector: `button:has(icon-broom)`, a broom icon that's GoMining's own icon for this button) if it's not already on cooldown.
- No passwords are ever stored. Auth is via session cookies captured through a one-time live login (see the `capture-cookies` skill).
- After every successful run, the script re-saves that account's current cookies back to its GitHub secret (`persist_refreshed_cookies()`). **This is required, not optional**: GoMining rotates its `refresh_token` on use, so a static cookie saved once will work exactly once and then permanently fail with a login redirect.
- `REPO` (used for the self-refresh `gh secret set` call) and the account list are both derived at runtime, not hardcoded; see `GITHUB_REPOSITORY` (set automatically by GitHub Actions) and `GOMINING_ACCOUNT_LABELS` in the workflow env. This is what makes the repo fork-portable.
- Each account gets up to `MAX_ATTEMPTS` (3) tries within a single run, `RETRY_DELAY_SECONDS` (10) apart, before being reported as failed (added 2026-08-20, see "Retry logic" below). The one exception is an explicit `/login` redirect: that's treated as a dead session and returned immediately without retrying, since a rejected session fails identically every time and retrying it just burns ~90 seconds for nothing.

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

Fix: `run_for_account()` now retries the navigate-and-click sequence up to `MAX_ATTEMPTS` (3) times in-process, `RETRY_DELAY_SECONDS` (10s) apart, before giving up. This catches short-lived hiccups (slow page load, flaky network) within the same run instead of losing an entire day to them. It deliberately does *not* apply to the explicit `/login`-redirect case, that's a real dead session, not a fluke, and retrying it 3 times just wastes ~90 seconds confirming what we already know.

Knock-on effects of this change, all already applied:
- Job-level `timeout-minutes` in `maintenance.yml` bumped 3 → 6 to cover the new worst-case (2 accounts, both retrying the full 3 attempts).
- Sentry's `max_runtime` in `main()` bumped 5 → 8 to match, so Sentry doesn't flag a legitimately-still-retrying run as stuck.
- A `FAILED` result (and the resulting GitHub email) now means an account struck out 3 times, not once, this is a good thing (fewer false-alarm emails for things that self-resolve) but means a "FAILED" email is a stronger signal that something's actually wrong.

## GitHub CLI multi-account gotcha

If you have more than one `gh` login on your machine and `git push` / `gh` commands fail with "Repository not found" on a repo you can see exists, that's the tell: GitHub returns that error (not "forbidden") when the *active* account can't see a private repo, rather than confirming its existence to an unauthorized account. Check `gh auth status` for which account is active, and run `gh auth switch` / `gh auth setup-git` as needed (the latter because Windows' Git Credential Manager doesn't automatically follow `gh auth switch`). Pushing to `.github/workflows/` also specifically requires the `workflow` OAuth scope (`gh auth refresh -h github.com -s workflow` if a push gets rejected for missing scope).

## Secrets in this repo

- `GOMINING_COOKIES_<LABEL>` (one per account in `GOMINING_ACCOUNT_LABELS`): session cookies (JSON array), self-refreshed by the script every run. See the `capture-cookies` skill to re-capture from scratch.
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

## If a scheduled run fails

GitHub emails on failure. Check the run log first: "redirected to login" means a session expired; use the `capture-cookies` skill for that account. Failures also upload a screenshot + HTML snapshot as a workflow artifact (`debug-artifacts`) for anything less obvious. If `SENTRY_DSN` is set, also check the Sentry project for grouped/historical error data.
