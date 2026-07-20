# Breda RNI Appointment Checker

Checks https://breda.mijnafspraakmaken.nl (RNI appointment, product=45) for
open slots in August 2026, and emails you when new ones appear. Runs
automatically on GitHub's servers every 15 minutes — your laptop does not
need to be open.

## How it decides "available"

The site doesn't have a public API, so this script loads the real page with
a headless browser (Playwright) and reads the calendar: days you can click
are "available," greyed-out/disabled days are not. Every appointment site
builds this slightly differently, so **the first run or two may need a
small tweak** — see "If it doesn't work" below. That's normal for this kind
of scraper, not a sign anything is broken.

## One-time setup (about 10 minutes)

### 1. Create a GitHub account (if you don't have one)
Free, at https://github.com/signup.

### 2. Create a new repository
- Go to https://github.com/new
- Name it anything (e.g. `rni-checker`)
- Set it to **Public** (so scheduled Actions runs are free/unlimited — see
  note below). Nothing sensitive lives in this repo.
- Click "Create repository"

### 3. Upload these files
On the new repo's page, click "Add file" → "Upload files," and drag in
everything from this folder (keeping the `.github/workflows/check.yml`
path intact — GitHub should preserve folder structure if you drag the
whole folder in a modern browser; otherwise use `git` from a terminal:

```
cd rni-checker
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/rni-checker.git
git push -u origin main
```

### 4. Create a Gmail "app password" to send from
(You can use any email provider that supports SMTP — Gmail is the easiest
to set up.)

1. Turn on 2-Step Verification on the Gmail account you'll send from:
   https://myaccount.google.com/signinoptions/two-step-verification
2. Create an app password: https://myaccount.google.com/apppasswords
   - App: "Mail", Device: "Other" → name it "RNI checker"
   - Copy the 16-character password it gives you

### 5. Add secrets to your GitHub repo
In your repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add these three (SMTP_SERVER/SMTP_PORT are optional — they default
to Gmail):

| Name            | Value                                      |
|-----------------|---------------------------------------------|
| `EMAIL_FROM`    | the Gmail address you made the app password for |
| `EMAIL_PASSWORD`| the 16-character app password (no spaces)   |
| `EMAIL_TO`      | the email address you want notified (can be the same one) |

### 6. Turn on Actions
Go to the **Actions** tab in your repo → you should see "Check RNI Breda
appointments" → click "I understand my workflows, go ahead and enable
them" if prompted.

### 7. Test it manually
Actions tab → "Check RNI Breda appointments" → "Run workflow" → Run. Click
into the run after ~1-2 minutes to see the log output and confirm it
completed. If August dates are already available, you should get an email
within a minute or two of the run finishing.

That's it — from here it runs itself every 15 minutes.

## If it doesn't work

If a run fails, you'll get one email saying so (it won't spam you every 15
minutes), and the run will have "debug-files" attached (a screenshot +
saved HTML of the page it saw) under the run's "Artifacts" section at the
bottom of the run page. Paste what those show back to me and I can adjust
the selectors in `check_appointments.py` — appointment widgets like this
one change their exact markup from time to time, so this may need
occasional small fixes.

## Notes

- **Public repo = free.** GitHub Actions is unlimited on public repos. If
  you make the repo private instead, scheduled runs use your free monthly
  minutes (2,000 min/month on the free plan) — at ~1-2 min per run every 15
  minutes, that's roughly 3,000-6,000 min/month, which would exceed the
  free private-repo quota. Keep it public, or reduce the schedule (e.g.
  every 30-60 min) if you'd rather keep it private.
- Nothing personal/sensitive is stored in the repo — just which day-numbers
  were seen as available.
- Runs every 15 minutes on GitHub's clock, which is UTC; times may drift a
  few minutes under GitHub's load, which is normal and not a problem here.
- You can change `EARLIEST_RELEVANT_DAY`, `TARGET_MONTH`/`TARGET_YEAR`, or
  the cron schedule directly in the files if your plans change.
