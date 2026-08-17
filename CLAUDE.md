# gomining-automation

Automates tapping GoMining's daily "maintenance" button for one or more accounts (configured via `GOMINING_ACCOUNT_LABELS`) so their maintenance-discount streak never lapses, without needing a laptop to be on. Runs on GitHub Actions. See `README.md` for user-facing setup instructions — this file is operational/maintainer notes.

## How it works

- `gomining_maintenance.py` — for each configured account, loads that account's saved session cookies into a headless Playwright browser, opens the dashboard, and clicks the maintenance button (selector: `button:has(icon-broom)` — a broom icon is GoMining's own icon for this button) if it's not already on cooldown.
- No passwords are ever stored. Auth is via session cookies captured through a one-time live login (see the `capture-cookies` skill).
- After every successful run, the script re-saves that account's current cookies back to its GitHub secret (`persist_refreshed_cookies()`). **This is required, not optional** — GoMining rotates its `refresh_token` on use, so a static cookie saved once will work exactly once and then permanently fail with a login redirect.
- `REPO` (used for the self-refresh `gh secret set` call) and the account list are both derived at runtime, not hardcoded — see `GITHUB_REPOSITORY` (set automatically by GitHub Actions) and `GOMINING_ACCOUNT_LABELS` in the workflow env. This is what makes the repo fork-portable.

## The reset mechanic (important, learned the hard way)

The maintenance discount resets on a **fixed UTC calendar-day boundary (00:00 UTC)**, not a rolling 24h cooldown from your last click (confirmed via GoMining's own FAQ: https://help.nft.gomining.com/faq/maintenance-fees-and-discounts). The countdown timer shown in the UI is always counting down to the *same* daily reset point, not to "24h after you clicked." **Missing an entire UTC day resets the whole accumulated discount streak to zero**, not just that day's increment — so reliability matters more than it might first appear.

## GitHub Actions scheduling gotchas

1. **The `on.schedule` cron can get "stuck."** GitHub's workflow registration doesn't always pick up edits to the schedule on a normal push. Symptom: you push a new cron, but runs keep firing (or not firing) on the old schedule, and `gh api repos/OWNER/REPO/actions/workflows/ID` shows `updated_at` frozen at the original creation time despite multiple pushes. **Fix: after ANY change to `on.schedule`, run (from inside the repo directory — `gh` infers the repo from the git remote, no `--repo` flag needed):**
   ```
   gh workflow disable "Daily GoMining Maintenance"
   gh workflow enable "Daily GoMining Maintenance"
   ```
   Confirm it worked by checking `updated_at` moved to just now (`gh api repos/{owner}/{repo}/actions/workflows/{workflow_id} --jq '{state, updated_at}'` — get the workflow ID from `gh workflow list`).
2. **GitHub's scheduler is best-effort.** Expect 15–45 minute delays past the target time, and occasionally a dropped slot entirely — this is documented GitHub behavior (schedule events can be delayed or dropped under load, especially at `:00`), not a bug here. This is why the schedule runs multiple attempts rather than a single exact time.
3. Current schedule lives in `.github/workflows/maintenance.yml` — it's been iterated on a lot; check `git log` for the reasoning before changing it again.

## GitHub CLI multi-account gotcha

If you have more than one `gh` login on your machine and `git push` / `gh` commands fail with "Repository not found" on a repo you can see exists, that's the tell — GitHub returns that error (not "forbidden") when the *active* account can't see a private repo, rather than confirming its existence to an unauthorized account. Check `gh auth status` for which account is active, and run `gh auth switch` / `gh auth setup-git` as needed (the latter because Windows' Git Credential Manager doesn't automatically follow `gh auth switch`). Pushing to `.github/workflows/` also specifically requires the `workflow` OAuth scope (`gh auth refresh -h github.com -s workflow` if a push gets rejected for missing scope).

## Secrets in this repo

- `GOMINING_COOKIES_<LABEL>` (one per account in `GOMINING_ACCOUNT_LABELS`) — session cookies (JSON array), self-refreshed by the script every run. See the `capture-cookies` skill to re-capture from scratch.
- `GH_PAT_SECRETS_WRITE` — fine-grained PAT scoped to only this repo, Secrets: read/write, nothing else. Used by the script to call `gh secret set` and self-refresh the cookie secrets above.

## If a scheduled run fails

GitHub emails on failure. Check the run log first — "redirected to login" means a session expired; use the `capture-cookies` skill for that account. Failures also upload a screenshot + HTML snapshot as a workflow artifact (`debug-artifacts`) for anything less obvious.
