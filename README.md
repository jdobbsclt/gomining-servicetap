# GoMining ServiceTap

Automatically taps GoMining's daily "maintenance" (service) button for one or
more accounts, so your maintenance-discount streak never lapses — even if
your computer is off. Runs on GitHub Actions (free tier is plenty). Also
includes an optional weekly job that re-extends your veGOMINING lock(s) so
their votes don't decay (see "Weekly lock re-extend" below).

## How it works

- Runs several times a day via GitHub Actions, cloud-hosted
- Authenticates with saved browser session cookies — never your password
- Skips gracefully if the button's already on cooldown
- Self-refreshing: GoMining rotates the login on every use, so each run
  saves the new one back automatically. Without this, a captured login
  works once and then fails for good.
- A session that truly dies fails the run and GitHub emails you

**Why it runs multiple times a night:** the discount resets on a fixed UTC
calendar-day boundary (00:00 UTC), not a rolling 24 hours from your last
click ([GoMining's FAQ](https://help.nft.gomining.com/faq/maintenance-fees-and-discounts)).
Missing a whole UTC day resets the entire streak, not just that day.
GitHub's scheduler is best-effort and can skip a slot, so several
independent attempts a night meaningfully cut the odds of missing one
entirely.

## One-time setup

### The wizard (recommended)

[Open the setup wizard](https://jdobbsclt.github.io/gomining-servicetap/setup.html)
and follow it. It signs you into GitHub (the same device-flow sign-in the
`gh` CLI uses — you approve on GitHub's own site), forks this repo, walks
you through a one-click bookmark that grabs your GoMining login, writes your
secrets, and runs a real test. No GitHub token to create — it sets your
fork to keep the refreshed login in GitHub's own encrypted Actions cache
instead. Your GitHub sign-in and GoMining login never touch each other.

**If it ever breaks:** sign into the wizard again with the same GitHub
account. It recognizes your existing fork and goes straight to fixing it —
a fresh login, or waking a paused schedule — instead of trying to fork
again.

The rest of this section is the same setup by hand, for anyone who'd
rather skip the wizard or see what it's doing.

### 1. Fork this repo

Click **Fork**, not "Use this template" — forking keeps the one-click
**Sync fork** button working later (see "Staying up to date").

### 2. Set your account label(s)

Edit `.github/workflows/maintenance.yml`:

```yaml
env:
  GOMINING_ACCOUNT_LABELS: MAIN
  GOMINING_COOKIES_MAIN: ${{ secrets.GOMINING_COOKIES_MAIN }}
```

`MAIN` is just an example label — any short name works as long as it
matches between the two lines. For more than one account, comma-separate
the labels and add a matching `GOMINING_COOKIES_<LABEL>` line for each:

```yaml
env:
  GOMINING_ACCOUNT_LABELS: PRIMARY,SECONDARY
  GOMINING_COOKIES_PRIMARY: ${{ secrets.GOMINING_COOKIES_PRIMARY }}
  GOMINING_COOKIES_SECONDARY: ${{ secrets.GOMINING_COOKIES_SECONDARY }}
```

### 3. Capture your session cookies

Never your password — just the two session cookies (`access_token`,
`refresh_token`) that keep you logged in.

**Easiest: the cookie tool.**
[Open it](https://jdobbsclt.github.io/gomining-servicetap/cookie-tool.html)
(also built into the wizard) and drag the bookmark to your bar, once. Log
into GoMining in a private window, click the bookmark, and the login is on
your clipboard — paste it into step 4. No DevTools.

A third cookie, `cf_clearance`, isn't needed: it's Cloudflare's own (no
webpage can read it), and a GitHub Actions runner gets issued its own
automatically — verified 2026-09-27 from a runner with no prior login at
all. If a run ever does report a Cloudflare block, add one by hand (see
the manual method below).

**Using Claude Code?** The `capture-cookies` skill
(`.claude/skills/capture-cookies/SKILL.md`) drives a real browser for you —
you complete the Google login yourself, Claude pushes the session straight
to the right secret. Ask Claude to "capture cookies for an account." Also
the fix for a "session expired" failure. (A scripted, no-browser version of
this used to ship as `recapture.py`; Google now blocks automated Google
logins outright, so it was removed.)

**Manual method (any browser, no bookmarklet):**
1. Log into <https://app.gomining.com>
2. DevTools (F12) → **Application** → **Storage → Cookies** →
   `https://app.gomining.com`
3. Note `access_token` and `refresh_token` (`cf_clearance` only if a run
   reports a Cloudflare block — see above)
4. Build a JSON array:
   ```json
   [{"name": "access_token", "value": "...", "domain": ".gomining.com", "path": "/", "expires": 1234567890, "httpOnly": false, "secure": true, "sameSite": "Lax"}]
   ```
   (`domain`/`httpOnly`/`secure`/`sameSite` are columns in the same
   DevTools table.)

### 4. Add the GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret.**

- One `GOMINING_COOKIES_<LABEL>` secret per account, the JSON array from
  step 3

GoMining rotates the login on every use, so something has to save the new
one back each run — pick one:

**No token (what the wizard sets up).** In `maintenance.yml` (and
`lock_extend.yml` if you use it), swap the `GH_TOKEN` line for:
```yaml
COOKIE_STORE: cache
COOKIE_CACHE_KEY: ${{ secrets.COOKIE_CACHE_KEY }}
```
`COOKIE_CACHE_KEY` has to be a real Fernet key, not any random string (the
script checks and refuses to run on a bad one):
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
— paste the output exactly. The refreshed login then lives, encrypted with
that key, in GitHub's own Actions cache. Nothing to renew.

**Personal access token (the original method).** A fine-grained
`GH_PAT_SECRETS_WRITE` token, scoped to only this repo, **Secrets: read and
write** and nothing else.
[Pre-filled token form](https://github.com/settings/personal-access-tokens/new?name=ServiceTap+self-refresh&expires_in=365&secrets=write)
— still switch **Repository access** to "Only select repositories" and
pick your fork yourself; the form defaults to "All repositories," which
would let the token touch every repo you own. Expires in a year — set a
reminder.

### 5. Test it

**Actions** tab → "Daily Service Button Tap" → **Run workflow**. Check the
log for `OK` on every account.

### 6. Let it run

Fully automated from here, on the schedule in `maintenance.yml`.

**Give it a day or two before worrying.** GitHub's scheduler is
best-effort — a first run can lag or not fire at all (ours once took over
24 hours), and it settles down once the schedule's existed a while. Same
adjustment period after any edit to the cron. See `CLAUDE.md` if runs go
quiet.

## Optional: Sentry error monitoring

Works fine without it. What it adds: searchable error history, and — the
real reason to bother — catching a schedule that **never fires at all**,
which produces no email since nothing started.

1. Create a free Sentry project, grab its DSN
2. Add it as repo secret `SENTRY_DSN`
3. Done — `gomining_maintenance.py` picks it up next run

## Optional: Weekly veGOMINING lock re-extend

GoMining's Governance → My Lock lets you push a lock's end date back to the
platform max; its votes (and the discount days it covers) decay if you
don't, roughly weekly. `lock_extend.yml` automates that click, reusing the
same saved session as the daily tap.

**Ships live — disable it if you don't want it.** It's written for this
repo's own `PRIMARY,SECONDARY` layout, so a fresh fork running it fails and
emails you. The wizard disables it on your copy automatically; if you
forked by hand, turn it off yourself — **Actions** tab → "Weekly Lock
Re-extend" → **⋯** → **Disable workflow**.

To use it:
1. In `lock_extend.yml`, set the account labels to match `maintenance.yml`,
   then re-enable the workflow. Default schedule is Saturday ~11am ET —
   edit the `cron` line to change it.
2. Optional: `LOCK_SKIP_THRESHOLD_<LABEL>: "<GMT amount>"` in the `env:`
   block leaves any position on that account under that many GMT alone
   (e.g. a small dust position). Unset = every position gets re-extended.
3. Test before trusting the schedule: **Actions** tab → "Weekly Lock
   Re-extend" → **Run workflow**. Confirm `Lock re-extend: OK` in the log
   and that the position actually moved on GoMining's own page.

Since this is a real financial action, not a routine click:
- Each position's new state is verified against GoMining's own account
  data (days-to-expire must have actually grown) — a click sequence that
  "succeeds" without moving the real lock counts as a failure.
- One position failing doesn't stop the others in the same run; failures
  are reported the same way a tap failure is.
- It navigates straight to each position's edit page from the API's own
  position list, not by clicking through My Lock's row markup (which could
  change without notice).
- A position already at (or within about a week of) the max is left alone
  and counted OK — GoMining's own page drops the "Max" option at that
  point. A 0-GMT (closed) position is always skipped.

## Staying up to date

Your fork doesn't update itself.

- **One-off:** open your fork, click **Sync fork**. It merges
  automatically unless an update touches the same lines you edited in
  `maintenance.yml`'s `env:` block, in which case GitHub asks you to
  resolve it by hand (usually trivial).
- **Get notified:** on
  [the upstream repo](https://github.com/jdobbsclt/gomining-servicetap),
  **Watch → Custom → Releases**.

Updates only ever touch the script, docs, and occasionally the schedule —
never your Secrets.

## If a run fails

Each account gets 3 attempts per run before it's reported failed, so a
one-off hiccup usually resolves itself silently. GitHub emails you once a
run actually exhausts its attempts. Check the log:

- **"session expired"**: that account's session is dead. Fix: recapture
  cookies. Set up with the wizard? Sign in again — it goes straight to a
  "refresh your login" screen, no re-forking. Otherwise: step 3 above.
  GoMining invalidates sessions on their end sometimes; this is expected
  occasionally, not a bug.
- **"session expired" right after enabling 2FA** (on GoMining or the
  Google account you sign into): expected once — 2FA invalidates existing
  sessions. Recapture, and every run after is normal. The nightly tap
  reuses a saved session and never logs in, so 2FA has no ongoing effect
  on it.
- **Anything else**: a screenshot + HTML snapshot at the moment of failure
  are uploaded as a `debug-artifacts` artifact.

A schedule that never fires produces no notification at all — that's what
Sentry (above) is for.

## Maintainer notes

See `CLAUDE.md` for the operational gotchas: GitHub's `schedule` trigger
can get "stuck" and needs a disable/re-enable cycle after editing the
cron, its timing is best-effort, and there's a multi-account `gh` CLI
quirk worth knowing.

The wizard (`docs/setup.html`) has its own tiny backend,
`setup-wizard/worker/` (a Cloudflare Worker) — see its own README.

The cookie bookmarklet appears on two pages (`docs/setup.html`,
`docs/cookie-tool.html`), both built from one source: edit
`setup-wizard/bookmarklet/bookmarklet.src.js`, then run
`python setup-wizard/bookmarklet/build.py` (`--check` to verify both pages
are current). Never hand-edit the `javascript:` link.

## What storing session cookies actually means

- GitHub encrypts secrets at rest and never displays them again, to
  anyone, through any interface — they exist as plain text only for the
  seconds a run is actually executing, inside GitHub's own runner.
- These cookies are a live session, not your password. Someone with them
  could act as your logged-in account for as long as the cookies stay
  valid — like a stolen "stay signed in" session — but couldn't log in
  fresh, change your password, or pass GoMining's account recovery.
- Only someone with push access to *your* fork could ever extract one (by
  adding a workflow step that deliberately reveals it). The thing actually
  worth protecting is your GitHub account itself — a strong password and
  2FA.
- Suspect a leak? Recapture fresh cookies and overwrite the secret, same
  as you'd treat a stolen "remember me" session.

Standard risk for anything that automates a logged-in session on your
behalf — not unique to this repo.

## A note on GoMining's terms

We read GoMining's [Terms of Use](https://gomining.com/terms) directly.
Automation is prohibited in a few places, but each is scoped to a specific
feature — Bonus Miner rewards (2.6.6), the Miner Wars "Spell Bot" (4.3.6),
the AI Assistant (8.4.7d), raffle/contest entries (10.3) — and the
maintenance discount itself (3.1–3.2) carries no automation restriction
anywhere in the document. Automating the daily tap also appears to be a
fairly common, openly-discussed practice
([example browser extension](https://gist.github.com/magicdude4eva/11a9b24e2066a5f0198c6df241d5059f)).

That said, Section 42 reserves the right to terminate any account "for any
other reason or no reason" — a standard broad clause, independent of any
specific rule. Not legal advice; terms change; check GoMining's current
ToS yourself.

## License

MIT. See `LICENSE`.
