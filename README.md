# job-watch

A completely free automation that scans for new job and internship opportunities and emails me when it finds something. It runs on GitHub Actions (free tier), so it doesn't cost anything to keep running once it's set up.

## What it does

- Opens every link in `companies.json` with a headless browser (Playwright), so even job pages that load their content with JavaScript work fine.
- For job board search pages (`job_boards`): pulls out every individual posting and compares it against what it saw last run. Anything new goes into the report.
- For company career pages (`diff_watch`): takes a hash of the page's visible content, and if it changed since last time, flags it as "worth checking manually" — this works on any site since it doesn't need to know the page's internal layout.
- If anything new turns up, it sends me a single summary email with the details and links.
- It saves the last-seen state in `data/state.json`, which the workflow updates automatically on every run — I never touch this file by hand.

## Before the first real run

I built this with well-established patterns (Playwright + BeautifulSoup), but I haven't run it against every one of these sites under real conditions. So there's a small chance the first live run needs a minor fix if one of these sites has changed its layout. If a source errors out, the email report (or the Actions log) will say exactly which one and why.

## One-time setup

### 1. Repo and files

Done — files are uploaded to this repo, including `.github/workflows` and `data`.

### 2. Gmail App Password

1. Turn on 2-Step Verification on my Google account: myaccount.google.com/security
2. Go to: myaccount.google.com/apppasswords
3. Create a new App Password (name it "job-watch"), and copy the 16-character code it generates. This is separate from my regular Google password.

### 3. Repository secrets

On this repo's page: Settings → Secrets and variables → Actions → New repository secret. Add three:

| Name | Value |
|---|---|
| `EMAIL_ADDRESS` | my Gmail address |
| `EMAIL_APP_PASSWORD` | the 16-character code from step 2 |
| `TO_EMAIL` | where alerts should land (can be the same address) |

### 4. Test run

Actions tab → "job-watch" in the sidebar → "Run workflow" to trigger it immediately instead of waiting for the schedule. The first run only builds a baseline, so it won't email about every job that currently exists — only new ones that show up after that.

## Adding or removing sources

Edit `companies.json` directly, no code changes needed:

- `diff_watch`: add a link to any company's careers page, even without a structured search — any change gets flagged.
- `job_boards`: add a search URL from any job board (jobs.ps, Bayt.com, RemoteOK, We Work Remotely, etc.) — whatever search link shows up in the browser can be pasted straight in here.

## Changing the schedule

In `.github/workflows/watch.yml`:
```
- cron: "0 6,18 * * *"
```
This runs at 6am and 6pm UTC (roughly 8-9am / 8-9pm Palestine time). Change it to whatever schedule fits — crontab.guru is useful for figuring out the syntax.

## Cost

Zero. GitHub Actions free tier easily covers two short runs a day, Gmail SMTP is free, and there's no paid API or subscription anywhere in this pipeline.
