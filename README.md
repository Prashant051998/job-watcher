# job-watcher

A CLI tool that watches **200+ US tech-company career pages** and emails you whenever a new role matching your interests shows up.

It's not a generic web scraper — it knows about applicant-tracking systems (Workday, Greenhouse, Lever, Ashby, iCIMS, SmartRecruiters, Eightfold, Oracle HCM), uses their **public JSON APIs** where available, and falls back to a stealth-mode headless browser only when it has to. State is kept locally in SQLite so you only ever get alerted about jobs you haven't seen before.

Out of the box it's tuned for **OS / kernel / embedded / firmware / platform / graphics / backend** roles in the US, but every filter is a short regex list at the top of `career_watcher.py` — edit them once and you're done.

## What it does

- **Watches ~250 companies** across silicon, big tech, cloud infra, Linux vendors, networking, autonomous/robotics, aerospace, gaming, storage, AI labs, fintech, dev tools, security, and more (see the `COMPANIES` list — fully editable).
- **Auto-detects the ATS** behind each career site and calls the right backend:
  - Workday → JSON API (`/wday/cxs/.../jobs`)
  - Greenhouse → `boards-api.greenhouse.io`
  - Lever → `api.lever.co`
  - Ashby → `jobs.ashbyhq.com/api/non-user-graphql`
  - Oracle HCM (Cloud Recruiting) → `hcmRestApi/resources/.../recruitingCEJobRequisitions`
  - **SimplifyJobs/New-Grad-Positions** — parses the community-maintained markdown README on GitHub
  - Anything else → headless Chromium via Playwright + `playwright-stealth`
- **Filters titles** through three regex layers: include if it looks like a software role (`ROLE_PATTERN`), exclude obvious non-engineering noise (`EXCLUDE_PATTERN`), then require either an entry-level marker, a general-SWE keyword, or a domain keyword (OS/kernel/embedded/firmware/...).
- **US-only**: includes if it sees a US state/city/abbreviation; excludes if it sees a non-US indicator (India, UK, EMEA, APAC, etc.).
- **Verifies every URL is alive** (HTTP 200 / 3xx) before sending an alert, so you don't get a digest full of 404s.
- **Sends two kinds of emails**:
  - **Real-time alert** the moment a new job clears all filters.
  - **Daily digest** at a configurable hour, even if nothing new came in (so you know the watcher is alive).
- **Persists `seen_jobs.db`** so restarts don't re-alert on jobs you've already seen.

## Quick start

### 1. Install dependencies

```bash
pip install playwright beautifulsoup4 playwright-stealth requests
playwright install chromium
```

### 2. Set up your email

The tool emails you the alerts. Easiest path is a Gmail account with an [App Password](https://myaccount.google.com/apppasswords) (requires 2FA on the account).

Set these environment variables — they're never read from source:

```bash
export EMAIL_FROM="you@gmail.com"
export EMAIL_TO="you@gmail.com"      # can be a different inbox
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your-16-char-app-password"
# Optional — defaults shown:
# export SMTP_HOST="smtp.gmail.com"
# export SMTP_PORT="587"
```

For a different provider, just change `SMTP_HOST` / `SMTP_PORT`.

### 3. Run it

```bash
python career_watcher.py
```

It will:
1. Open `seen_jobs.db` (created on first run).
2. Fan out across all companies — up to `CONCURRENCY` (default 8) in parallel.
3. For every new role that passes the filters and verifies, log it, store it, and email you.
4. Sleep `CHECK_INTERVAL_MINUTES` (default 30) and repeat.

`Ctrl+C` to stop. The DB persists, so you can stop and restart any time without getting re-spammed.

## Customizing it for *your* job search

Everything is in `career_watcher.py` — no config file, no env vars beyond email. Open the file and edit these blocks:

| Block | What it controls | Line |
|---|---|---|
| `COMPANIES` | The list of companies + URLs to watch | ~80 |
| `ROLE_ANCHORS` | What counts as a software role at all | ~510 |
| `DOMAIN_KEYWORDS` | The keywords that mark a role as "interesting" (OS / kernel / embedded / firmware / graphics / backend, etc.) | ~520 |
| `EXCLUDE_KEYWORDS` | Roles you never want to hear about (sales, recruiter, designer, PM, frontend, …) | ~570 |
| `ENTRY_PATTERN` | New-grad / intern / junior signals — passes any role through even without a domain match | ~650 |
| `GENERAL_SWE_PATTERN` | Generic backend / SWE titles that should pass on their own | ~660 |
| `US_INDICATORS` / `NON_US_INDICATORS` | Location filter | ~580 |
| `CHECK_INTERVAL_MINUTES` | How often to poll | 67 |
| `DAILY_DIGEST_HOUR` | Hour (24h, local time) to send the keep-alive digest | 72 |
| `CONCURRENCY` | Pages fetched in parallel | 68 |

### Tuning the keyword filter

For example, to add machine-learning roles, drop these into `DOMAIN_KEYWORDS`:

```python
r"\bmachine learning\b", r"\bml engineer\b", r"\bllm\b",
r"\bdeep learning\b", r"\bcomputer vision\b",
```

To watch a new company, append to `COMPANIES`:

```python
{"name": "YourCompany", "url": "https://boards.greenhouse.io/yourcompany"},
```

If the URL is a Workday / Greenhouse / Lever / Ashby / Oracle / iCIMS / SmartRecruiters board, ATS auto-detection picks it up. Otherwise it falls back to headless-browser scraping.

## Architecture

```
                       ┌──────────────────────────┐
                       │ COMPANIES (list of URLs) │
                       └────────────┬─────────────┘
                                    │
                                    ▼
                       ┌──────────────────────────┐
                       │     detect_ats(url)      │
                       └────┬─────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   Workday API       Greenhouse API        ... or Playwright
   Lever API         Ashby GraphQL         (headless Chromium
   Oracle HCM        SimplifyJobs           + stealth, BS4 parse)
                            │
                            ▼
                ┌────────────────────────┐
                │ extract_jobs() →       │
                │   [{title, url, loc}]  │
                └───────────┬────────────┘
                            ▼
                ┌────────────────────────┐
                │ is_target_job(title)   │   ← ROLE / DOMAIN / EXCLUDE / ENTRY / SWE regexes
                │ is_us_location(loc)    │   ← US / NON-US regexes
                └───────────┬────────────┘
                            ▼
                ┌────────────────────────┐
                │ already in seen_jobs?  │   ← SQLite (DB_PATH)
                └───────────┬────────────┘
                            │ new only
                            ▼
                ┌────────────────────────┐
                │ verify URL (HTTP HEAD) │   ← drops ~30% stale/redirect links
                └───────────┬────────────┘
                            ▼
                ┌────────────────────────┐
                │ send_email(new_jobs)   │   ← SMTP, HTML body + .txt attachment
                └────────────────────────┘
```

## Email output

Each alert email contains:
- **HTML body** — title, company, location, "Apply" link, plus seniority hint extracted from the title.
- **Plain-text attachment** (`new_jobs.txt`) — same data as a list of URLs, ready to bulk-add to a tracker.

The daily digest at `DAILY_DIGEST_HOUR` is the same format, even if it's empty — that way silence still means *"watcher is healthy, just nothing new"*, not *"watcher crashed three days ago"*.

## File layout

```
job-watcher/
├── career_watcher.py     The whole thing — 1 file, ~1,400 lines.
├── README.md             You are here.
├── .gitignore            Keeps seen_jobs.db and editor cruft out of git.
└── seen_jobs.db          Created on first run. Holds the (company, url) tuples
                          you've already been notified about.
```

## Tips

- **First run will be loud.** Every passing job is "new" the first time. Either expect a giant email, or seed the DB once by running with the email functions stubbed and then enabling them.
- **Some companies block headless browsers.** Most major sites are fine because the tool hits their ATS API directly; the few that need full-browser rendering get `playwright-stealth` applied, but the most aggressively bot-walled (a handful of consumer sites) may still return empty.
- **Run it as a long-lived background process.** A `tmux` / `screen` session works; or wrap it in `launchd` (macOS) / `systemd` (Linux) for unattended operation.
- **Tune `CHECK_INTERVAL_MINUTES` up, not down.** 30 minutes is already overkill for most career pages; going below 10 min won't surface jobs faster but will increase your odds of getting rate-limited.

## Stack

- Python 3 (stdlib `asyncio`, `sqlite3`, `smtplib`, `re`)
- [Playwright](https://playwright.dev/python/) + [playwright-stealth](https://pypi.org/project/playwright-stealth/) for browser fallback
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) for HTML parsing
- [requests](https://requests.readthedocs.io/) for direct API calls and URL verification
- SQLite (no schema migrations — single table, single state file)
