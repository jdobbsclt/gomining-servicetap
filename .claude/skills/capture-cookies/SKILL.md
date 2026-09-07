---
name: capture-cookies
description: Capture a fresh GoMining session and save it as this repo's GitHub secret. Use when a scheduled run fails with "session expired" (or the older "redirected to login"), when setting up a new account for the first time, or when the user asks to "recapture cookies" / "refresh the session" / "re-login" for a GoMining account.
disable-model-invocation: true
---

# Capture GoMining Session Cookies

This repo's automation authenticates via saved session cookies, not passwords (see `CLAUDE.md`). When a session dies (a "session expired" line in a run log — or the page showing GoMining's signup screen instead of the dashboard), get a fresh one and update the corresponding GitHub secret. This must be done live, with the actual account owner present — only they can complete the Google login step; you never see or ask for their password or 2FA code.

## Preferred path: `recapture.py`

If the user is at their own machine and just wants this fixed, point them at the repo's `recapture.py` — it does everything below in one command:

```
python recapture.py                 # both accounts
python recapture.py SECONDARY        # just one
```

It needs `playwright` + chromium installed locally and `gh` logged in with push access. Use the manual procedure below only when driving it yourself through the Playwright MCP (e.g. the user can see the browser you control but can't run the script).

## Manual procedure (Playwright MCP)

1. Confirm which account needs recapturing if not already clear — check `GOMINING_ACCOUNT_LABELS` in `.github/workflows/maintenance.yml` for the configured labels (e.g. `MAIN` → secret `GOMINING_COOKIES_MAIN`).
2. Open a live browser to the login page: `mcp__playwright__browser_navigate` → `https://app.gomining.com/login`. If the browser might still hold a previous session's cookies (recapturing a second account in the same session), clear them first via `browser_run_code_unsafe`: `await page.context().clearCookies()`, then navigate again.
3. Tell the user the browser is ready and ask them to log in themselves via "Continue with Google." Wait for their confirmation. Do not screenshot while they are on Google's pages.
4. Confirm login actually succeeded — via `browser_run_code_unsafe`, navigate to `https://app.gomining.com/nft-miners`, wait a few seconds, and check that the maintenance button renders (`button:has(icon-broom)` visible) and that an `access_token` cookie is present. GoMining no longer redirects logged-out visitors to `/login`, so a URL check is not enough — verify a logged-in signal.
5. Extract the relevant cookies via `browser_run_code_unsafe`, keeping only the standard Playwright fields (drop `partitionKey` / `_crHasCrossSiteAncestor`, which can break `add_cookies` on import):
   ```js
   async (page) => {
     const keep = ['access_token', 'refresh_token', 'cf_clearance'];
     const fields = ['name','value','domain','path','expires','httpOnly','secure','sameSite'];
     return (await page.context().cookies())
       .filter(c => c.domain.includes('gomining.com') && keep.includes(c.name))
       .map(c => Object.fromEntries(fields.map(f => [f, c[f]])));
   }
   ```
   A healthy capture has all three names, including `access_token` and `refresh_token`. (GoMining's cookie set changed on 2026-09-06 — the older `brwsr` / `irtps` / `sa-user-id*` / `viewport` cookies are gone. Keep this list in sync with `KEEP_COOKIE_NAMES` in `gomining_maintenance.py` and `recapture.py`.)
6. Save the returned JSON array to a local scratch file (never inside this repo — it must never be committed) using the session's scratchpad directory, then push it straight to the secret (from inside the repo directory so `gh` infers the repo from the git remote):
   ```
   gh secret set GOMINING_COOKIES_<LABEL> < <scratch-file-path>
   ```
   If `gh` is logged in as more than one account, make sure the one with push access to this repo is active first (`gh auth switch`).
7. Close the browser (`mcp__playwright__browser_close`) and delete the scratch file.
8. Verify: `gh workflow run "Daily Service Button Tap"`, then check the run log shows that account succeeding instead of "session expired".

## Notes

- Never echo the raw cookie values in a chat message — they're live bearer credentials for gomining.com. Tool call results aren't shown to the user directly (only your text responses are), so working with them via tool calls is fine; just don't paste them into your own reply.
- If `gh` push/API calls fail with "Repository not found" on a repo that clearly exists, see the GitHub CLI multi-account note in `CLAUDE.md` before proceeding further.
