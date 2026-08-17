---
name: capture-cookies
description: Capture a fresh GoMining session and save it as this repo's GitHub secret. Use when a scheduled run fails with "redirected to login" (session expired), when setting up a new account for the first time, or when the user asks to "recapture cookies" / "refresh the session" / "re-login" for a GoMining account.
disable-model-invocation: true
---

# Capture GoMining Session Cookies

This repo's automation authenticates via saved session cookies, not passwords (see `CLAUDE.md`). When a session dies (a "redirected to login" line in a run log), use this procedure to get a fresh one and update the corresponding GitHub secret. This must be done live, with the actual account owner present — only they can complete the Google login step; you never see or ask for their password or 2FA code.

## Steps

1. Confirm which account needs recapturing if not already clear — check `GOMINING_ACCOUNT_LABELS` in `.github/workflows/maintenance.yml` for the configured labels (e.g. `MAIN` → secret `GOMINING_COOKIES_MAIN`).
2. Open a live browser to the login page: `mcp__playwright__browser_navigate` → `https://app.gomining.com/login`. If this browser might still hold a previous session's cookies, clear them first via `browser_run_code_unsafe`: `await page.context().clearCookies()`, then navigate again.
3. Tell the user the browser is ready and ask them to log in themselves via "Continue with Google." Wait for their confirmation.
4. Confirm login actually succeeded — via `browser_run_code_unsafe`, run `page.waitForLoadState('networkidle')` then return `page.url()`. It should be `https://app.gomining.com/nft-miners`, not still `/login`.
5. Extract the relevant cookies via `browser_run_code_unsafe`:
   ```js
   async (page) => {
     const cookies = await page.context().cookies();
     const keep = ['cf_clearance','brwsr','irtps','access_token','refresh_token','sa-user-id','sa-user-id-v2','sa-user-id-v3','viewport'];
     return cookies.filter(c => c.domain.includes('gomining.com') && keep.includes(c.name));
   }
   ```
6. Save the returned JSON array to a local scratch file (never inside this repo — it must never be committed) using the session's scratchpad directory, then push it straight to the secret (run from inside the repo directory so `gh` infers the repo from the git remote — no `--repo` flag needed):
   ```
   gh secret set GOMINING_COOKIES_<LABEL> < <scratch-file-path>
   ```
7. Close the browser (`mcp__playwright__browser_close`).
8. Optionally verify it worked: `gh workflow run maintenance.yml`, then check the next run's log shows that account succeeding instead of redirecting to login.

## Notes

- Never echo the raw cookie values in a chat message — they're live bearer credentials for gomining.com. Tool call results aren't shown to the user directly (only your text responses are), so working with them via tool calls is fine; just don't paste them into your own reply.
- If `gh` push/API calls fail with "Repository not found" on a repo that clearly exists, see the GitHub CLI multi-account note in `CLAUDE.md` before proceeding further.
