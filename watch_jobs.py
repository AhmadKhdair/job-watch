#!/usr/bin/env python3
"""
job-watch: a free, self-hosted (via GitHub Actions) job-opportunity watcher.

What it does:
  1. Renders each URL in companies.json with a headless browser (so JS-heavy
     career pages work too, not just plain HTML).
  2. For "job_boards" entries: extracts individual job posting links + titles,
     and compares them against what was seen last run (data/state.json).
     Anything new gets reported.
  3. For "diff_watch" entries: scans the visible page text for lines that
     mention training / internship / train-to-hire opportunities (regardless
     of exact wording), and compares that set against last run. Only newly
     appeared training-related lines get reported — not every change on the
     page. This works on any site without needing to know its layout, and
     without flooding the inbox over unrelated page edits.
  4. If anything new/changed was found, sends a single summary email via
     Gmail SMTP (credentials come from environment variables / GitHub Secrets
     — this script never stores or hardcodes them).
  5. Saves the new state to data/state.json so next run only reports NEW
     things (the workflow commits this file back to the repo).

Nothing here costs money: GitHub Actions free tier, Gmail SMTP, no APIs.
"""

import json
import hashlib
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "companies.json"
STATE_PATH = ROOT / "data" / "state.json"

# Words that show up in navigation/footer links but are never real job
# postings — used to filter noise out of extracted links.
NAV_NOISE = {
    "home", "jobs", "sign in", "register", "for employers", "find jobs",
    "post a job", "companies", "articles", "contact us", "job seeker",
    "job alerts", "training courses", "all jobs", "browse jobs",
    "browse companies", "browse resumes", "employers", "career menu",
    "log in", "create your profile", "resources", "premium", "about us",
    "privacy policy", "cookie policy", "terms of use", "sitemap", "rss",
    "get jobs by email", "share page", "connect", "people",
    "view company profile", "post a remote job", "top 100 remote companies",
    "top trending remote jobs", "all other jobs", "sign up",
}

# A job-posting link almost always ends in some kind of id/slug and lives
# under one of these path fragments across the sites we support.
JOB_LINK_PATTERNS = [
    r"/jobs/[\w-]+", r"/job/[\w-]+", r"/o/[\w-]+", r"/en/jobs/[\w-]+",
    r"/remote-jobs/[\w-]+",  # RemoteOK + We Work Remotely posting links
]

# Used to filter diff_watch alerts down to training / internship /
# train-to-hire mentions only, regardless of the exact wording a company
# uses for it. Matches English and Arabic variants.
TRAINING_KEYWORDS_RE = re.compile(
    r"\btrain(?:ees?|ing|eeship)?\b"
    r"|\bintern(?:s|ship)?\b"
    r"|train[\s-]?to[\s-]?hire"
    r"|تدريب|متدرب",
    re.IGNORECASE,
)


def log(msg):
    print(msg, flush=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"job_boards": {}, "diff_watch": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def render_page(browser, url, timeout_ms=25000):
    """Load a URL with a real (headless) browser and return (html, text)."""
    page = browser.new_page(user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ))
    try:
        page.goto(url, timeout=timeout_ms, wait_until="networkidle")
    except Exception:
        # Some sites never go fully idle (analytics beacons etc.) — a
        # partial load is still usually good enough for our purposes.
        pass
    html = page.content()
    text = page.inner_text("body") if page.query_selector("body") else ""
    page.close()
    return html, text


def extract_job_links(base_url, html):
    """Pull out (title, absolute_url) pairs that look like job postings."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = " ".join(a.get_text(" ", strip=True).split())
        if not text or len(text) < 4:
            continue
        if text.lower() in NAV_NOISE:
            continue
        if not any(re.search(pat, href) for pat in JOB_LINK_PATTERNS):
            continue
        abs_url = href if href.startswith("http") else _urljoin(base_url, href)
        # Keep the longest/most descriptive text seen for a given link.
        if abs_url not in found or len(text) > len(found[abs_url]):
            found[abs_url] = text
    return found  # {url: title}


def extract_training_snippets(text):
    """Return the set of visible lines on a page that mention training,
    internship, or train-to-hire opportunities — used to filter diff_watch
    alerts down to just those, instead of any page edit."""
    snippets = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line or len(line) > 300:
            continue
        if TRAINING_KEYWORDS_RE.search(line):
            snippets.add(line)
    return snippets


def _urljoin(base, href):
    from urllib.parse import urljoin
    return urljoin(base, href)


def hash_text(text):
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def run():
    config = load_config()
    state = load_state()
    new_state = {"job_boards": dict(state.get("job_boards", {})),
                 "diff_watch": dict(state.get("diff_watch", {}))}

    report_new_jobs = []   # list of (source_name, title, url)
    report_changed_pages = []  # list of (source_name, snippet, url)
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # --- Job boards: structured extraction, diff by individual link ---
        for src in config.get("job_boards", []):
            name, url = src["name"], src["url"]
            try:
                html, _text = render_page(browser, url)
                links = extract_job_links(url, html)
                seen_before = set(state.get("job_boards", {}).get(url, []))
                seen_now = set(links.keys())

                new_links = seen_now - seen_before
                is_first_run = len(seen_before) == 0
                if not is_first_run:
                    for link in new_links:
                        report_new_jobs.append((name, links[link], link))

                new_state["job_boards"][url] = sorted(seen_now)
                log(f"[job_board] {name}: {len(seen_now)} postings seen, "
                    f"{len(new_links) if not is_first_run else 0} new "
                    f"({'first run, establishing baseline' if is_first_run else 'checked'})")
            except Exception as e:
                errors.append(f"{name} ({url}): {e}")
                log(f"[job_board] ERROR on {name}: {e}")

        # --- Company pages: only alert on new training/internship/
        # --- train-to-hire mentions, not on every page edit.
        for src in config.get("diff_watch", []):
            name, url = src["name"], src["url"]
            try:
                _html, text = render_page(browser, url)
                current_snippets = extract_training_snippets(text)

                previous_raw = state.get("diff_watch", {}).get(url)
                # Old state format stored a single hash string. If we see
                # that, treat it as no baseline yet rather than crashing.
                previous_snippets = (
                    set(previous_raw) if isinstance(previous_raw, list) else None
                )
                is_first_run = previous_snippets is None

                if not is_first_run:
                    new_snippets = current_snippets - previous_snippets
                    for snippet in new_snippets:
                        report_changed_pages.append((name, snippet, url))

                new_state["diff_watch"][url] = sorted(current_snippets)
                new_count = (
                    len(current_snippets - previous_snippets) if not is_first_run else 0
                )
                log(f"[diff_watch] {name}: {len(current_snippets)} training-related "
                    f"line(s) seen, {new_count} new "
                    f"({'first run, establishing baseline' if is_first_run else 'checked'})")
            except Exception as e:
                errors.append(f"{name} ({url}): {e}")
                log(f"[diff_watch] ERROR on {name}: {e}")

        browser.close()

    save_state(new_state)

    if report_new_jobs or report_changed_pages or errors:
        send_email_report(report_new_jobs, report_changed_pages, errors)
    else:
        log("Nothing new this run. No email sent.")


def send_email_report(new_jobs, changed_pages, errors):
    email_address = os.environ.get("EMAIL_ADDRESS")
    email_password = os.environ.get("EMAIL_APP_PASSWORD")
    to_email = os.environ.get("TO_EMAIL", email_address)
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not email_address or not email_password:
        log("EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set — printing report "
            "instead of emailing it.")
        print_report(new_jobs, changed_pages, errors)
        return

    lines = []
    if new_jobs:
        lines.append("New job postings found:\n")
        for source, title, url in new_jobs:
            lines.append(f"  • [{source}] {title}\n    {url}\n")
    if changed_pages:
        lines.append("\nNew training / internship / train-to-hire mentions found on company pages:\n")
        for source, snippet, url in changed_pages:
            lines.append(f"  • [{source}] {snippet}\n    {url}\n")
    if errors:
        lines.append("\nSources that failed to check this run (site may have changed structure):\n")
        for e in errors:
            lines.append(f"  • {e}\n")

    body = "\n".join(lines)
    subject = f"job-watch: {len(new_jobs)} new posting(s), {len(changed_pages)} training mention(s)"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_address
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(email_address, email_password)
        server.sendmail(email_address, [to_email], msg.as_string())

    log(f"Email sent to {to_email}: {subject}")


def print_report(new_jobs, changed_pages, errors):
    if new_jobs:
        print("NEW JOB POSTINGS:")
        for source, title, url in new_jobs:
            print(f"  [{source}] {title} -> {url}")
    if changed_pages:
        print("NEW TRAINING / INTERNSHIP / TRAIN-TO-HIRE MENTIONS:")
        for source, snippet, url in changed_pages:
            print(f"  [{source}] {snippet} -> {url}")
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"Fatal error in job-watch: {exc}", file=sys.stderr)
        raise
