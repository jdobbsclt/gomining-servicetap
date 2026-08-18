# GoMining ServiceTap

Automatically taps GoMining's daily "maintenance" (service) button for one or
more accounts, so your maintenance-discount streak never lapses, even if
your computer is off. Runs entirely on GitHub Actions (free tier is plenty).

## How it works

- Runs several times a day via GitHub Actions (cloud-hosted, works even if
  your PC is off)
- Authenticates using saved browser session cookies, **never your password**
- Skips gracefully if the button is already on cooldown for the day
- **Self-refreshing**: after every successful run, it saves the account's
  *current* cookies back to the secret. This matters: GoMining appears to
  rotate its refresh token on use, so a cookie captured once and never
  updated will work exactly once and then permanently fail.
- If a saved session ever truly expires, the run fails and GitHub
  automatically emails you

### The reset mechanic

The maintenance discount resets on a **fixed UTC calendar-day boundary
(00:00 UTC)**, not a rolling 24-hour cooldown from your last click, confirmed
via [GoMining's own FAQ](https://help.nft.gomining.com/faq/maintenance-fees-and-discounts).
Missing an entire UTC day resets your whole accumulated streak, not just that
day's increment, which is why this runs multiple times a day rather than
once: GitHub's own scheduler is documented as best-effort and can delay or
occasionally drop an individual run, so more independent attempts per day
meaningfully lowers the odds of a full miss.

## One-time setup

### 1. Fork or clone this repo

Push it to your own GitHub account, private or public, doesn't matter.

### 2. Set your account label

#### Automating one account

Most people only need to automate one account. Edit
`.github/workflows/maintenance.yml`:

```yaml
env:
  GOMINING_ACCOUNT_LABELS: MAIN
  GOMINING_COOKIES_MAIN: ${{ secrets.GOMINING_COOKIES_MAIN }}
```

`MAIN` is just an example. Any short label works, it just has to match
between the two lines.

#### Automating more than one account

Add a comma-separated label for each account, plus a matching
`GOMINING_COOKIES_<LABEL>` line:

```yaml
env:
  GOMINING_ACCOUNT_LABELS: PRIMARY,SECONDARY
  GOMINING_COOKIES_PRIMARY: ${{ secrets.GOMINING_COOKIES_PRIMARY }}
  GOMINING_COOKIES_SECONDARY: ${{ secrets.GOMINING_COOKIES_SECONDARY }}
```

This repo's own `.github/workflows/maintenance.yml` is actually configured
this way already (two real accounts); check it out as a working example of
the multi-account case. You'll repeat step 3 below once per account.

### 3. Capture your account's session cookies

You never enter your GoMining password anywhere in this repo or its
secrets, only session cookies, captured from a real logged-in browser.

**If you're using Claude Code:** this repo ships a `capture-cookies` skill
(`.claude/skills/capture-cookies/SKILL.md`) that walks Claude through doing
this for you interactively. Just ask it to capture cookies for an account.

**Manual method (any browser):**
1. Log into <https://app.gomining.com> normally
2. Open DevTools (F12) → **Application** tab → **Storage → Cookies** →
   `https://app.gomining.com`
3. Note the values for these cookie names: `cf_clearance`, `brwsr`,
   `irtps`, `access_token`, `refresh_token`, `sa-user-id`, `sa-user-id-v2`,
   `sa-user-id-v3`, `viewport`
4. Build a JSON array from them, one object per cookie, matching this shape:
   ```json
   [{"name": "access_token", "value": "...", "domain": ".gomining.com", "path": "/", "expires": 1234567890, "httpOnly": false, "secure": true, "sameSite": "Lax"}]
   ```
   (`domain`/`httpOnly`/`secure`/`sameSite` are visible as columns in the
   same DevTools cookie table.)

### 4. Add the GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository
secret**.

- One `GOMINING_COOKIES_<LABEL>` secret per account, paste the JSON array
  from step 3
- `GH_PAT_SECRETS_WRITE`, a **fine-grained** GitHub personal access token,
  scoped to **only this repo**, with **Secrets: read and write** permission
  and nothing else. This is what lets the script self-refresh the cookie
  secrets above after each run. Create one at
  <https://github.com/settings/tokens?type=beta>.

### 5. Test it

**Actions** tab → "Daily Service Button Tap" → **Run workflow**. Check the
log: you want to see `OK` for every account, not `FAILED`.

### 6. Let it run

From here it's fully automated on the schedule in
`.github/workflows/maintenance.yml`.

**Give it a day or two before worrying.** It's normal (not a sign anything's
misconfigured) for the first scheduled run(s) after setup to be late or not
fire at all. GitHub's scheduler is documented as best-effort (individual runs
can lag 15-45+ minutes, or occasionally skip a slot), and in our own testing
the very first scheduled workflow we ever created took over 24 hours to fire
even once. It reliably got more consistent once the schedule had simply
existed, untouched, for a while. This is exactly why the workflow runs 6
times a night instead of once: one flaky slot doesn't matter when 5 more are
coming. The same adjustment period tends to happen again after *any* edit to
the schedule, not just the first setup. See `CLAUDE.md` if you change it and
runs seem to go quiet for a bit.

### 7. (Optional) Add Sentry error monitoring

The script and workflow work fully without this. It's just better
visibility. Two things Sentry adds that GitHub's own failure emails can't:
searchable error history/trends across runs, and, the more important one,
detecting when the schedule **fails to fire at all**. GitHub only emails you
about a run that started and failed; a run that never started produces
nothing. Sentry's cron monitor alerts if no check-in arrives within the
expected window, closing that gap.

1. Create a free Sentry project (any org) and grab its DSN
2. Add it as a repository secret named `SENTRY_DSN`
3. That's it: `gomining_maintenance.py` picks it up automatically next run

If you skip this, everything still works, you just rely on GitHub's
failure emails alone, which is exactly what this repo ran on for a while.

## If a run fails

GitHub emails you automatically on any run that starts and fails. Check the
run log first:

- **"redirected to login"**: that account's session expired. Recapture its
  cookies (step 3 above) and update its secret.
- **Anything else**: a screenshot + HTML snapshot of the page at the moment
  of failure are uploaded as a `debug-artifacts` workflow artifact, to help
  figure out what actually happened.

If a scheduled run doesn't fire *at all* (no email, nothing in the Actions
tab), that's invisible to GitHub's own notifications by design. See step 7
above (Sentry) if you want to catch that case too.

## Maintainer notes (if you're editing this repo, not just using it)

See `CLAUDE.md` for operational gotchas learned the hard way: GitHub's
`schedule` trigger can get "stuck" and needs a disable/re-enable cycle after
editing the cron, its timing is best-effort by design, and there's a
multi-account `gh` CLI quirk worth knowing about.

## A note on GoMining's terms

Automating your own account's daily maintenance tap appears to be a fairly
common, openly-discussed practice in the GoMining community (see e.g.
[this browser extension that does the same thing](https://gist.github.com/magicdude4eva/11a9b24e2066a5f0198c6df241d5059f)),
but this isn't legal advice: check GoMining's current Terms of Service
yourself before relying on this.

## License

MIT. See `LICENSE`.
