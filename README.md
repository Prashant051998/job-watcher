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

## Use it for yourself — step by step

The tool ships **tuned for systems / kernel / embedded / firmware / backend roles in the US**. If that's *not* you, the only changes you need are: edit two keyword lists, optionally edit the companies list, plug in your email. Everything below walks through it in order.

### Step 1 — Clone the repo and `cd` in

```bash
git clone https://github.com/Prashant051998/job-watcher.git
cd job-watcher
```

You need Python 3.8 or newer:

```bash
python3 --version    # should print 3.8 or higher
```

### Step 2 — Install the Python packages it needs

```bash
pip install playwright beautifulsoup4 playwright-stealth requests
```

Then have Playwright download its headless browser (one-time, ~150 MB):

```bash
playwright install chromium
```

If you're curious what the script imports under the hood:

| Package | Purpose | Source |
|---|---|---|
| `asyncio`, `json`, `re`, `smtplib`, `email.mime.*`, `sqlite3`, `os`, `urllib.parse`, `datetime` | async loop, JSON parsing, regex filters, sending email, local DB, env vars | Python standard library — no install needed |
| `requests` | direct HTTP calls to ATS APIs + URL verification | `pip install requests` |
| `bs4` (BeautifulSoup) | parsing HTML when an ATS isn't detected | `pip install beautifulsoup4` |
| `playwright.async_api` | headless Chromium for JS-heavy career pages | `pip install playwright` + `playwright install chromium` |
| `playwright_stealth` | makes the headless browser harder to detect | `pip install playwright-stealth` *(optional — the script keeps working without it)* |

### Step 3 — Get an email password

The tool sends you the alerts over SMTP. Gmail is the easiest:

1. Turn on 2-Step Verification on your Google account.
2. Go to https://myaccount.google.com/apppasswords
3. Generate an **App Password** named e.g. *"job-watcher"*.
4. Copy the 16-character password it shows you (it's shown only once).

Using something other than Gmail? Just have your SMTP host / port / username / password handy. STARTTLS on port 587 works for most providers.

### Step 4 — Set the email environment variables

In the **same terminal** you'll run the script from:

```bash
export EMAIL_FROM="you@gmail.com"          # the address alerts are sent from
export EMAIL_TO="you@gmail.com"            # where alerts land (can be a different inbox)
export SMTP_USER="you@gmail.com"           # SMTP login (usually same as EMAIL_FROM)
export SMTP_PASS="xxxxxxxxxxxxxxxx"        # the 16-char Gmail App Password
```

For a non-Gmail provider also set:

```bash
export SMTP_HOST="smtp.your-provider.com"
export SMTP_PORT="587"
```

To make these persistent across terminal sessions, add the `export` lines to your `~/.zshrc` (macOS / zsh) or `~/.bashrc` (Linux).

### Step 5 — Tell the tool which *roles* you want

Open `career_watcher.py`. The two lists you care about live around lines 520 and 570:

**`DOMAIN_KEYWORDS`** (line ~520) — titles you *want* to hear about. Default is systems-heavy. Replace with regex patterns for your field.

> *Example — if you're a frontend developer:*
> ```python
> DOMAIN_KEYWORDS = [
>     r"\bfrontend\b", r"\bfront[- ]end\b",
>     r"\breact\b", r"\bvue\b", r"\bangular\b", r"\bnext\.?js\b",
>     r"\btypescript\b", r"\bjavascript\b",
>     r"\bui engineer\b", r"\bweb developer\b",
> ]
> ```

> *Example — if you're a data / ML engineer:*
> ```python
> DOMAIN_KEYWORDS = [
>     r"\bmachine learning\b", r"\bml engineer\b", r"\bdata engineer\b",
>     r"\bdata scientist\b", r"\bllm\b", r"\bdeep learning\b",
>     r"\bcomputer vision\b", r"\bnlp\b", r"\bmlops\b",
>     r"\bpytorch\b", r"\btensorflow\b",
> ]
> ```

**`EXCLUDE_KEYWORDS`** (line ~570) — titles you *never* want. The default already excludes things like recruiter / sales / PM / designer. **Important:** the default also excludes `frontend` (because the tool ships tuned for backend). If you *are* a frontend dev, **remove `frontend` and `front[- ]end` from this list** or your roles will get filtered out.

### Step 6 — Tell it which *companies* to watch

Around line 80 you'll find `COMPANIES`, a long list of dicts:

```python
COMPANIES = [
    {"name": "NVIDIA",       "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"},
    {"name": "AMD",          "url": "https://careers.amd.com/careers-home/jobs"},
    ...
]
```

- **To add a company**, append a new dict at the end. URLs from these systems auto-detect and use the fast API path:
  - Workday: `https://*.myworkdayjobs.com/...`
  - Greenhouse: `https://boards.greenhouse.io/<slug>` or `https://job-boards.greenhouse.io/<slug>`
  - Lever: `https://jobs.lever.co/<slug>`
  - Ashby: `https://jobs.ashbyhq.com/<slug>`
  - Oracle HCM: `https://*.fa.*.oraclecloud.com/...`
  - Any other URL: falls back to headless-browser scraping.
- **To remove a company**, delete its line (or comment it out with `#`).
- **Want a smaller list to start with?** Cut it down to 5–10 companies for your first run while you tune the filters, then expand.

### Step 7 — Adjust the location filter (only if you're not in the US)

The defaults at line ~580 only let through jobs that look US-based. If you want a different country:

- Swap `US_INDICATORS` for cities / states in your country.
- Move the US entries into `NON_US_INDICATORS` (or just leave `NON_US_INDICATORS` empty if you want jobs anywhere).

To turn the location filter **off entirely**, find `is_us_location` (line ~676) and have it `return True` unconditionally.

### Step 8 — Tweak the cadence (optional)

Top of the file (lines ~67–74):

```python
CHECK_INTERVAL_MINUTES = 30   # how often to poll all companies
CONCURRENCY = 8               # parallel pages fetched at once
DAILY_DIGEST_HOUR = 8         # send a keep-alive digest at 8am local time
ATTACH_TXT_FILE = True        # attach a .txt of URLs to each email
VERIFY_URLS = True            # HTTP HEAD each job link before alerting (drops dead links)
```

Don't drop `CHECK_INTERVAL_MINUTES` below 10 — you'll just get rate-limited without seeing jobs any faster.

### Step 9 — Do a test run

```bash
python career_watcher.py
```

You'll see log output as it visits each company. **The very first run treats every passing job as "new"** and will try to email a huge digest. Two ways to avoid that:

- **Option A — Just let it.** Get one giant email, archive it, from then on you only see true diffs.
- **Option B — Seed the DB silently first.** Temporarily comment out the `send_email(new_jobs)` line near the bottom of `check_once` (around line 1370), run once to populate `seen_jobs.db`, then uncomment and run for real.

Stop the script any time with `Ctrl+C`. The state in `seen_jobs.db` persists — restart whenever and you won't re-receive old alerts.

### Step 10 — Run it long-term

For continuous watching, run inside a `tmux` or `screen` session so it survives terminal closes:

```bash
tmux new -s jobs
python career_watcher.py
# Press Ctrl+B then D to detach. Reattach later with: tmux a -t jobs
```

Or set it up as a `launchd` (macOS) / `systemd` (Linux) service for true unattended operation.

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
