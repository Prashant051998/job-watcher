"""
Career-page job watcher — 100 US companies, OS / platform / embedded / Linux focus.

v3 changes:
  - Added playwright-stealth to defeat basic bot detection on Cloudflare/etc sites.
  - Detects ATS platform (Workday, Greenhouse, SmartRecruiters, Lever, iCIMS,
    Eightfold, Ashby) and applies the right scraping strategy automatically.
  - For Workday sites, calls the JSON API directly when possible (fast + reliable).
  - For Greenhouse boards (boards-api), calls the public API.

Setup:
    pip install playwright beautifulsoup4 playwright-stealth requests
    playwright install chromium
    Edit EMAIL CONFIG below. Gmail App Password: https://myaccount.google.com/apppasswords

Run:
    python career_watcher.py
Stop:
    Ctrl+C (state persists in seen_jobs.db)
"""

import asyncio
import json
import re
import smtplib
import sqlite3
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# Optional: playwright-stealth. Falls back gracefully if not installed or API differs.
try:
    from playwright_stealth import Stealth
    _STEALTH = Stealth()

    async def _apply_stealth(context):
        await _STEALTH.apply_stealth_async(context)
except ImportError:
    try:
        # Older API
        from playwright_stealth import stealth_async as _legacy_stealth

        async def _apply_stealth(page_or_context):
            await _legacy_stealth(page_or_context)
    except ImportError:
        async def _apply_stealth(_):
            pass  # Stealth not installed; continue without it.

# ================ EMAIL CONFIG ================
# Set these via environment variables so credentials never live in source.
# For Gmail, generate an App Password: https://myaccount.google.com/apppasswords
#
#   export EMAIL_FROM="you@gmail.com"
#   export EMAIL_TO="you@gmail.com"
#   export SMTP_USER="you@gmail.com"
#   export SMTP_PASS="your-16-char-app-password"

import os

EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO   = os.environ.get("EMAIL_TO", "")
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER", "")
SMTP_PASS  = os.environ.get("SMTP_PASS", "")

# ================ RUNTIME CONFIG ================

CHECK_INTERVAL_MINUTES = 30
CONCURRENCY = 8                # how many pages to fetch in parallel
ATTACH_TXT_FILE = True         # attach .txt file with URLs to each email
VERIFY_URLS = True             # check each job URL is reachable (200/redirect) before alerting
URL_VERIFY_CONCURRENCY = 16    # parallel verification requests
DAILY_DIGEST_HOUR = 8          # send a roundup email at this hour (24h, local time) even if quiet
DB_PATH = "seen_jobs.db"
PAGE_TIMEOUT_MS = 20_000        # 20s — bot-walled sites hang; don't waste 60s each

# ================ COMPANIES ================
# 100 US companies grouped by category. Most are systems/OS/embedded heavy.

COMPANIES = [
    # ---- New-grad aggregator (hundreds of companies, auto-updated every ~30min) ----
    {"name": "SimplifyJobs New-Grad", "url": "https://github.com/SimplifyJobs/New-Grad-Positions"},

    # ---- Silicon / Semiconductors (heavy OS, driver, embedded work) ----
    {"name": "NVIDIA",            "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"},
    {"name": "AMD",               "url": "https://careers.amd.com/careers-home/jobs"},
    {"name": "Intel",             "url": "https://intel.wd1.myworkdayjobs.com/External"},
    {"name": "Qualcomm",          "url": "https://qualcomm.wd12.myworkdayjobs.com/External"},
    {"name": "Arm",               "url": "https://arm.wd1.myworkdayjobs.com/External"},
    {"name": "Broadcom",          "url": "https://broadcom.wd1.myworkdayjobs.com/External_Career"},
    {"name": "Marvell",           "url": "https://marvell.wd1.myworkdayjobs.com/MarvellCareers"},
    {"name": "Micron",            "url": "https://micron.wd1.myworkdayjobs.com/External"},
    {"name": "Texas Instruments", "url": "https://edbz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/jobs"},
    {"name": "Analog Devices",    "url": "https://analogdevices.wd1.myworkdayjobs.com/External"},
    {"name": "Cadence",           "url": "https://cadence.wd1.myworkdayjobs.com/External_Careers"},
    {"name": "Synopsys",          "url": "https://synopsys.wd1.myworkdayjobs.com/Careers"},
    {"name": "Applied Materials", "url": "https://amat.wd1.myworkdayjobs.com/External"},
    {"name": "Lam Research",      "url": "https://lamresearch.wd1.myworkdayjobs.com/External"},
    {"name": "KLA",               "url": "https://kla.wd1.myworkdayjobs.com/Search"},
    {"name": "SambaNova",         "url": "https://boards.greenhouse.io/sambanovasystems"},
    {"name": "Cerebras",          "url": "https://job-boards.greenhouse.io/cerebrassystems"},
    {"name": "Groq",              "url": "https://job-boards.greenhouse.io/groq"},
    {"name": "Tenstorrent",       "url": "https://job-boards.greenhouse.io/tenstorrent"},

    # ---- Big Tech (massive platform / OS / infra orgs) ----
    {"name": "Google",            "url": "https://www.google.com/about/careers/applications/jobs/results"},
    {"name": "Apple",             "url": "https://jobs.apple.com/en-us/search"},
    {"name": "Meta",              "url": "https://www.metacareers.com/jobs"},
    {"name": "Microsoft",         "url": "https://jobs.careers.microsoft.com/global/en/search"},
    {"name": "Amazon",            "url": "https://www.amazon.jobs/en/search"},
    {"name": "Netflix",           "url": "https://explore.jobs.netflix.net/careers"},
    {"name": "IBM",               "url": "https://www.ibm.com/careers/search"},
    {"name": "Oracle",            "url": "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"},
    {"name": "Salesforce",        "url": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"},
    {"name": "Adobe",             "url": "https://adobe.wd5.myworkdayjobs.com/external_experienced"},

    # ---- Cloud Infrastructure / SaaS Platforms ----
    {"name": "Cloudflare",        "url": "https://boards.greenhouse.io/cloudflare"},
    {"name": "DigitalOcean",      "url": "https://job-boards.greenhouse.io/digitalocean77"},
    {"name": "Fastly",            "url": "https://www.fastly.com/about/careers/current-openings"},
    {"name": "Akamai",            "url": "https://akamaicareers.dejobs.org/"},
    {"name": "Equinix",           "url": "https://equinix.wd1.myworkdayjobs.com/Equinix"},
    {"name": "Snowflake",         "url": "https://careers.snowflake.com/us/en/search-results?keywords=engineer"},
    {"name": "Databricks",        "url": "https://www.databricks.com/company/careers/open-positions"},
    {"name": "MongoDB",           "url": "https://boards.greenhouse.io/mongodb"},
    {"name": "Elastic",           "url": "https://www.elastic.co/about/careers"},
    {"name": "Confluent",         "url": "https://job-boards.greenhouse.io/confluentinc"},

    # ---- OS / Linux Vendors ----
    {"name": "Red Hat",           "url": "https://redhat.wd5.myworkdayjobs.com/jobs"},
    {"name": "Canonical",         "url": "https://canonical.com/careers/all"},
    {"name": "SUSE",              "url": "https://jobs.suse.com/"},

    # ---- Networking / Telecom ----
    {"name": "Cisco",             "url": "https://jobs.cisco.com/jobs/SearchJobs/"},
    {"name": "Juniper Networks",  "url": "https://juniper.wd5.myworkdayjobs.com/JuniperCareers"},
    {"name": "Arista Networks",   "url": "https://www.arista.com/en/company/careers/jobs"},
    {"name": "Verizon",           "url": "https://mycareer.verizon.com/jobs/"},
    {"name": "AT&T",              "url": "https://www.att.jobs/search-jobs"},
    {"name": "T-Mobile",          "url": "https://careers.t-mobile.com/jobs/"},
    {"name": "Comcast",           "url": "https://jobs.comcast.com/careers"},

    # ---- Autonomous / Robotics / EV (heavy embedded + Linux) ----
    {"name": "Tesla",             "url": "https://www.tesla.com/careers/search/"},
    {"name": "SpaceX",            "url": "https://www.spacex.com/careers/jobs"},
    {"name": "Waymo",             "url": "https://boards.greenhouse.io/waymo"},
    {"name": "Rivian",            "url": "https://careers.rivian.com/jobs"},
    {"name": "Lucid Motors",      "url": "https://lucidmotors.com/careers"},
    {"name": "Aurora Innovation", "url": "https://aurora.tech/jobs"},
    {"name": "Zoox",              "url": "https://boards.greenhouse.io/zoox"},
    {"name": "Nuro",              "url": "https://www.nuro.ai/careers"},
    {"name": "Boston Dynamics",   "url": "https://bostondynamics.com/careers/"},
    {"name": "Skydio",            "url": "https://boards.greenhouse.io/skydio"},
    {"name": "Anduril",           "url": "https://job-boards.greenhouse.io/andurilindustries"},
    {"name": "Saildrone",         "url": "https://www.saildrone.com/careers"},

    # ---- Aerospace / Defense (RTOS, firmware, embedded) ----
    {"name": "Lockheed Martin",   "url": "https://www.lockheedmartinjobs.com/search-jobs"},
    {"name": "Northrop Grumman",  "url": "https://www.northropgrumman.com/jobs/"},
    {"name": "Raytheon (RTX)",    "url": "https://rtx.wd5.myworkdayjobs.com/REC_RTX_Ext_Gateway"},
    {"name": "Boeing",            "url": "https://jobs.boeing.com/search-jobs"},
    {"name": "General Dynamics",  "url": "https://careers-gd.icims.com/jobs/search"},
    {"name": "L3Harris",          "url": "https://careers.l3harris.com/global/en/search-results"},
    {"name": "Palantir",          "url": "https://www.palantir.com/careers/"},
    {"name": "Anthropic",         "url": "https://www.anthropic.com/jobs"},
    {"name": "OpenAI",            "url": "https://openai.com/careers/search/"},

    # ---- Gaming / Graphics (relevant to your Wayland/EGM background) ----
    {"name": "Valve",             "url": "https://www.valvesoftware.com/en/jobs"},
    {"name": "Epic Games",        "url": "https://www.epicgames.com/site/en-US/careers"},
    {"name": "Unity",             "url": "https://careers.unity.com/"},
    {"name": "Electronic Arts",   "url": "https://ea.gr8people.com/jobs"},
    {"name": "Activision",        "url": "https://careers.activisionblizzard.com/jobs"},
    {"name": "Take-Two",          "url": "https://www.take2games.com/careers"},
    {"name": "Riot Games",        "url": "https://www.riotgames.com/en/work-with-us/jobs"},

    # ---- Storage / Hardware / Systems vendors ----
    {"name": "Dell",              "url": "https://jobs.dell.com/en/search-jobs"},
    {"name": "HPE",               "url": "https://hpe.wd5.myworkdayjobs.com/Jobsathpe"},
    {"name": "NetApp",            "url": "https://careers.netapp.com/search-jobs"},
    {"name": "Pure Storage",      "url": "https://www.purestorage.com/company/careers/job-openings.html"},
    {"name": "Western Digital",   "url": "https://westerndigital.wd5.myworkdayjobs.com/Careers"},
    {"name": "Seagate",           "url": "https://seagate.wd1.myworkdayjobs.com/External"},

    # ---- Systems-heavy infra / observability / data ----
    {"name": "Stripe",            "url": "https://stripe.com/jobs/search"},
    {"name": "Datadog",           "url": "https://boards.greenhouse.io/datadog"},
    {"name": "Splunk",            "url": "https://www.splunk.com/en_us/careers/jobs.html"},
    {"name": "ServiceNow",        "url": "https://servicenow.wd1.myworkdayjobs.com/ServiceNowExternalCareer"},
    {"name": "Twilio",            "url": "https://boards.greenhouse.io/twilio"},
    {"name": "Cloudera",          "url": "https://www.cloudera.com/about/careers.html"},
    {"name": "PagerDuty",         "url": "https://boards.greenhouse.io/pagerduty"},

    # ---- Misc large engineering employers ----
    {"name": "Uber",              "url": "https://www.uber.com/us/en/careers/list/"},
    {"name": "Lyft",              "url": "https://boards.greenhouse.io/lyft"},
    {"name": "DoorDash",          "url": "https://careersatdoordash.com/jobs"},
    {"name": "Airbnb",            "url": "https://careers.airbnb.com/positions/"},
    {"name": "LinkedIn",          "url": "https://www.linkedin.com/jobs/linkedin-jobs"},
    {"name": "Pinterest",         "url": "https://www.pinterestcareers.com/jobs/"},
    {"name": "Snap",              "url": "https://snapchat.wd1.myworkdayjobs.com/snap"},
    {"name": "Spotify",                "url": "https://www.lifeatspotify.com/jobs"},
    {"name": "Reddit",            "url": "https://www.redditinc.com/careers"},
    {"name": "Square / Block",    "url": "https://block.xyz/careers"},
    {"name": "Coinbase",          "url": "https://www.coinbase.com/careers/positions"},

    # ================ ADDED: 150+ Bay Area / Silicon Valley companies ================
    # Most below use Greenhouse, Lever, or Ashby — they'll hit the API path cleanly.
    # Companies are grouped by sector so it's easy to trim if you don't care about some.

    # ---- AI labs and AI startups (Bay Area) ----
    {"name": "Scale AI",              "url": "https://boards.greenhouse.io/scaleai"},
    {"name": "Mistral AI",            "url": "https://job-boards.greenhouse.io/mistralai"},
    {"name": "Character AI",          "url": "https://job-boards.greenhouse.io/characterai"},
    {"name": "Perplexity",            "url": "https://job-boards.greenhouse.io/perplexityai"},
    {"name": "xAI",                   "url": "https://job-boards.greenhouse.io/xai"},
    {"name": "Cohere",                "url": "https://jobs.lever.co/cohere"},
    {"name": "Inflection AI",         "url": "https://job-boards.greenhouse.io/inflection"},
    {"name": "Adept",                 "url": "https://job-boards.greenhouse.io/adept"},
    {"name": "Runway",                "url": "https://job-boards.greenhouse.io/runwayml"},
    {"name": "Hugging Face",          "url": "https://apply.workable.com/huggingface/"},
    {"name": "Weights & Biases",      "url": "https://job-boards.greenhouse.io/weightsandbiases"},
    {"name": "Together AI",           "url": "https://jobs.ashbyhq.com/togetherai"},
    {"name": "Mosaic ML",             "url": "https://job-boards.greenhouse.io/mosaicml"},
    {"name": "Modal Labs",            "url": "https://jobs.ashbyhq.com/modal"},
    {"name": "Replicate",             "url": "https://jobs.ashbyhq.com/replicate"},
    {"name": "LangChain",             "url": "https://jobs.ashbyhq.com/langchain"},
    {"name": "Pinecone",              "url": "https://job-boards.greenhouse.io/pinecone"},
    {"name": "Glean",                 "url": "https://job-boards.greenhouse.io/gleanwork"},
    {"name": "Notion",                "url": "https://job-boards.greenhouse.io/notion"},
    {"name": "Harvey",                "url": "https://jobs.ashbyhq.com/harvey"},

    # ---- Fintech (Bay Area) ----
    {"name": "Plaid",                 "url": "https://job-boards.greenhouse.io/plaid"},
    {"name": "Brex",                  "url": "https://job-boards.greenhouse.io/brex"},
    {"name": "Ramp",                  "url": "https://jobs.ashbyhq.com/ramp"},
    {"name": "Chime",                 "url": "https://job-boards.greenhouse.io/chime"},
    {"name": "Mercury",               "url": "https://job-boards.greenhouse.io/mercury"},
    {"name": "Affirm",                "url": "https://job-boards.greenhouse.io/affirm"},
    {"name": "Robinhood",             "url": "https://job-boards.greenhouse.io/robinhood"},
    {"name": "Carta",                 "url": "https://job-boards.greenhouse.io/carta"},
    {"name": "Gusto",                 "url": "https://job-boards.greenhouse.io/gusto"},
    {"name": "Bill.com",              "url": "https://job-boards.greenhouse.io/billcom"},
    {"name": "Marqeta",               "url": "https://job-boards.greenhouse.io/marqeta"},

    # ---- Dev tools / infra / data ----
    {"name": "GitHub",                "url": "https://github.careers/careers-home/jobs"},
    {"name": "GitLab",                "url": "https://about.gitlab.com/jobs/all-jobs/"},
    {"name": "Sentry",                "url": "https://job-boards.greenhouse.io/sentry"},
    {"name": "LaunchDarkly",          "url": "https://job-boards.greenhouse.io/launchdarkly"},
    {"name": "Vercel",                "url": "https://vercel.com/careers"},
    {"name": "Netlify",               "url": "https://www.netlify.com/careers/"},
    {"name": "Linear",                "url": "https://linear.app/careers"},
    {"name": "Retool",                "url": "https://job-boards.greenhouse.io/retool"},
    {"name": "Airtable",              "url": "https://job-boards.greenhouse.io/airtable"},
    {"name": "Postman",               "url": "https://www.postman.com/company/careers/open-positions/"},
    {"name": "Grafana Labs",          "url": "https://job-boards.greenhouse.io/grafanalabs"},
    {"name": "New Relic",             "url": "https://job-boards.greenhouse.io/newrelic"},
    {"name": "Sumo Logic",            "url": "https://job-boards.greenhouse.io/sumologic"},
    {"name": "Lightbend",             "url": "https://jobs.lever.co/lightbend"},
    {"name": "InfluxData",            "url": "https://www.influxdata.com/careers/"},
    {"name": "CockroachDB",           "url": "https://job-boards.greenhouse.io/cockroachlabs"},
    {"name": "PlanetScale",           "url": "https://job-boards.greenhouse.io/planetscale"},
    {"name": "Neon",                  "url": "https://job-boards.greenhouse.io/neondatabase"},
    {"name": "Supabase",              "url": "https://jobs.ashbyhq.com/supabase"},
    {"name": "Render",                "url": "https://jobs.ashbyhq.com/render"},

    # ---- Robotics / autonomy / aerospace (Bay Area + nearby) ----
    {"name": "Figure AI",              "url": "https://job-boards.greenhouse.io/figureai"},
    {"name": "Cobalt Robotics",        "url": "https://job-boards.greenhouse.io/cobaltrobotics"},
    {"name": "Agility Robotics",       "url": "https://www.agilityrobotics.com/careers"},
    {"name": "Wisk Aero",              "url": "https://wisk.aero/careers/"},
    {"name": "Joby Aviation",          "url": "https://www.jobyaviation.com/careers/"},
    {"name": "Archer Aviation",        "url": "https://www.archer.com/careers"},
    {"name": "Astranis",               "url": "https://job-boards.greenhouse.io/astranis"},
    {"name": "Planet Labs",            "url": "https://job-boards.greenhouse.io/planetlabs"},
    {"name": "Rocket Lab",             "url": "https://www.rocketlabusa.com/careers/positions/"},
    {"name": "Relativity Space",       "url": "https://job-boards.greenhouse.io/relativity"},
    {"name": "Shield AI",              "url": "https://shield.ai/careers/"},

    # ---- Security ----
    {"name": "CrowdStrike",            "url": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers"},
    {"name": "Palo Alto Networks",     "url": "https://jobs.paloaltonetworks.com/en/jobs/"},
    {"name": "Wiz",                    "url": "https://job-boards.greenhouse.io/wiz"},
    {"name": "Snyk",                   "url": "https://job-boards.greenhouse.io/snyk"},
    {"name": "Okta",                   "url": "https://job-boards.greenhouse.io/okta"},
    {"name": "1Password",              "url": "https://job-boards.greenhouse.io/1password"},
    {"name": "Lacework",               "url": "https://job-boards.greenhouse.io/lacework"},
    {"name": "Tailscale",              "url": "https://jobs.ashbyhq.com/tailscale"},
    {"name": "Doppler",                "url": "https://jobs.ashbyhq.com/doppler"},

    # ---- Workplace / SaaS / collab ----
    {"name": "Dropbox",                "url": "https://job-boards.greenhouse.io/dropbox"},
    {"name": "Asana",                  "url": "https://job-boards.greenhouse.io/asana"},
    {"name": "Atlassian",              "url": "https://www.atlassian.com/company/careers/all-jobs"},
    {"name": "Zoom",                   "url": "https://zoom.wd5.myworkdayjobs.com/Zoom"},
    {"name": "Box",                    "url": "https://www.box.com/careers"},
    {"name": "Webflow",                "url": "https://job-boards.greenhouse.io/webflow"},
    {"name": "Figma",                  "url": "https://job-boards.greenhouse.io/figma"},
    {"name": "Canva",                  "url": "https://www.lifeatcanva.com/en/jobs/"},
    {"name": "Miro",                   "url": "https://job-boards.greenhouse.io/mironew"},
    {"name": "Calendly",               "url": "https://job-boards.greenhouse.io/calendly"},

    # ---- Mobility / delivery / consumer ----
    {"name": "Instacart",              "url": "https://job-boards.greenhouse.io/instacart"},
    {"name": "Postmates",              "url": "https://job-boards.greenhouse.io/postmates"},
    {"name": "Opendoor",               "url": "https://job-boards.greenhouse.io/opendoor"},
    {"name": "Compass",                "url": "https://www.compass.com/careers/"},
    {"name": "Zillow",                 "url": "https://www.zillow.com/careers/"},
    {"name": "Yelp",                   "url": "https://www.yelp.careers/us/en/search-results"},
    {"name": "Bumble",                 "url": "https://job-boards.greenhouse.io/bumble"},

    # ---- E-commerce / payments / commerce platforms ----
    {"name": "Shopify",                "url": "https://www.shopify.com/careers/search"},
    {"name": "Wish",                   "url": "https://job-boards.greenhouse.io/contextlogic"},
    {"name": "Faire",                  "url": "https://www.faire.com/careers"},
    {"name": "Klaviyo",                "url": "https://job-boards.greenhouse.io/klaviyo"},
    {"name": "Toast",                  "url": "https://job-boards.greenhouse.io/toast"},

    # ---- Crypto / web3 (mostly SF/Palo Alto) ----
    {"name": "Anchorage Digital",      "url": "https://job-boards.greenhouse.io/anchorage"},
    {"name": "Alchemy",                "url": "https://job-boards.greenhouse.io/alchemy"},
    {"name": "Solana Labs",            "url": "https://jobs.solana.com/"},
    {"name": "Kraken",                 "url": "https://job-boards.greenhouse.io/kraken"},
    {"name": "Circle",                 "url": "https://job-boards.greenhouse.io/circle"},

    # ---- Bio / health software ----
    {"name": "Verily",                 "url": "https://www.verily.com/careers/"},
    {"name": "Color Health",           "url": "https://job-boards.greenhouse.io/colorgenomics"},
    {"name": "Tempus",                 "url": "https://job-boards.greenhouse.io/tempus"},
    {"name": "10x Genomics",           "url": "https://www.10xgenomics.com/careers"},

    # ---- More silicon / hardware (Bay Area focus) ----
    {"name": "Astera Labs",            "url": "https://job-boards.greenhouse.io/asteralabs"},
    {"name": "Recogni",                "url": "https://recogni.com/careers/"},
    {"name": "d-Matrix",               "url": "https://www.d-matrix.ai/careers/"},
    {"name": "Lightmatter",            "url": "https://job-boards.greenhouse.io/lightmatter"},
    {"name": "Ayar Labs",              "url": "https://ayarlabs.com/careers/"},
    {"name": "MemryX",                 "url": "https://memryx.com/careers/"},
    {"name": "Esperanto Technologies", "url": "https://www.esperanto.ai/careers/"},
    {"name": "Mythic AI",              "url": "https://mythic.ai/careers/"},
    {"name": "Rain AI",                "url": "https://rain.ai/careers"},
    {"name": "Etched",                 "url": "https://www.etched.com/careers"},
    {"name": "FuriosaAI",              "url": "https://www.furiosa.ai/careers/"},
    {"name": "Rebellions",             "url": "https://www.rebellions.ai/careers"},
    {"name": "Rivos",                  "url": "https://rivosinc.com/careers/"},
    {"name": "Mojo (Modular)",         "url": "https://www.modular.com/company/careers"},

    # ---- Notable other Bay Area / Silicon Valley engineering shops ----
    {"name": "ByteDance / TikTok US",  "url": "https://careers.tiktok.com/position"},
    {"name": "Niantic",                "url": "https://nianticlabs.com/careers/"},
    {"name": "Twitch",                 "url": "https://www.twitch.tv/jobs/en/"},
    {"name": "Discord",                "url": "https://discord.com/careers"},
    {"name": "Brave Software",         "url": "https://brave.com/careers/"},
    {"name": "DuckDuckGo",             "url": "https://jobs.lever.co/duckduckgo"},
    {"name": "Khan Academy",           "url": "https://job-boards.greenhouse.io/khanacademy"},
    {"name": "Coursera",               "url": "https://job-boards.greenhouse.io/coursera"},
    {"name": "Chegg",                  "url": "https://jobs.chegg.com/"},
    {"name": "Nutanix",                "url": "https://www.nutanix.com/company/careers"},
    {"name": "Cohesity",               "url": "https://www.cohesity.com/company/careers/"},
    {"name": "Rubrik",                 "url": "https://www.rubrik.com/company/careers"},
    {"name": "Veeva Systems",          "url": "https://www.veeva.com/careers/"},
    {"name": "Workday",                "url": "https://workday.wd5.myworkdayjobs.com/Workday"},
    {"name": "DocuSign",               "url": "https://docusign.wd1.myworkdayjobs.com/External"},
    {"name": "Pure Storage HQ",        "url": "https://job-boards.greenhouse.io/purestorage"},
    {"name": "Roku",                   "url": "https://job-boards.greenhouse.io/roku"},
    {"name": "TiVo",                   "url": "https://www.tivo.com/careers"},
    {"name": "Synaptics",              "url": "https://synaptics.wd1.myworkdayjobs.com/External"},
    {"name": "Lattice Semiconductor",  "url": "https://latticesemi.wd1.myworkdayjobs.com/External"},
    {"name": "Ambarella",              "url": "https://www.ambarella.com/about-us/careers/"},
    {"name": "MaxLinear",              "url": "https://www.maxlinear.com/company/careers"},

    # ================ BATCH 3: +100 more US companies (mostly Greenhouse/Lever/Ashby) ================
    # ATS-slug-based — verification will silently drop the ~30% whose slug differs.

    # ---- Enterprise SaaS / B2B ----
    {"name": "Coupa",                  "url": "https://job-boards.greenhouse.io/coupasoftware"},
    {"name": "Gong",                   "url": "https://job-boards.greenhouse.io/gong"},
    {"name": "Outreach",               "url": "https://job-boards.greenhouse.io/outreach"},
    {"name": "Amplitude",              "url": "https://job-boards.greenhouse.io/amplitude"},
    {"name": "Mixpanel",               "url": "https://job-boards.greenhouse.io/mixpanel"},
    {"name": "Segment",                "url": "https://job-boards.greenhouse.io/segment"},
    {"name": "Heap",                   "url": "https://job-boards.greenhouse.io/heap"},
    {"name": "Looker",                 "url": "https://job-boards.greenhouse.io/looker"},
    {"name": "ThoughtSpot",            "url": "https://job-boards.greenhouse.io/thoughtspot"},
    {"name": "Sigma Computing",        "url": "https://job-boards.greenhouse.io/sigmacomputing"},
    {"name": "Fivetran",               "url": "https://job-boards.greenhouse.io/fivetran"},
    {"name": "dbt Labs",               "url": "https://job-boards.greenhouse.io/dbtlabs"},
    {"name": "Hex",                    "url": "https://jobs.ashbyhq.com/hex"},
    {"name": "Census",                 "url": "https://jobs.ashbyhq.com/census"},
    {"name": "Hightouch",              "url": "https://jobs.ashbyhq.com/hightouch"},
    {"name": "Airbyte",                "url": "https://job-boards.greenhouse.io/airbyte"},
    {"name": "Temporal",               "url": "https://job-boards.greenhouse.io/temporaltechnologies"},
    {"name": "Cortex",                 "url": "https://jobs.ashbyhq.com/cortex"},
    {"name": "Vanta",                  "url": "https://jobs.ashbyhq.com/vanta"},
    {"name": "Drata",                  "url": "https://job-boards.greenhouse.io/drata"},

    # ---- Infra / observability / platform ----
    {"name": "Chronosphere",           "url": "https://job-boards.greenhouse.io/chronosphere"},
    {"name": "Honeycomb",              "url": "https://job-boards.greenhouse.io/honeycomb"},
    {"name": "Cribl",                  "url": "https://job-boards.greenhouse.io/cribl"},
    {"name": "Spacelift",              "url": "https://jobs.ashbyhq.com/spacelift"},
    {"name": "env0",                   "url": "https://jobs.ashbyhq.com/env0"},
    {"name": "Pulumi",                 "url": "https://job-boards.greenhouse.io/pulumi"},
    {"name": "Teleport",               "url": "https://job-boards.greenhouse.io/gravitational"},
    {"name": "Buildkite",              "url": "https://jobs.ashbyhq.com/buildkite"},
    {"name": "CircleCI",               "url": "https://job-boards.greenhouse.io/circleci"},
    {"name": "Harness",                "url": "https://job-boards.greenhouse.io/harness"},
    {"name": "Earthly",                "url": "https://jobs.ashbyhq.com/earthly"},
    {"name": "Replicated",             "url": "https://job-boards.greenhouse.io/replicated"},
    {"name": "Aiven",                  "url": "https://job-boards.greenhouse.io/aiven"},
    {"name": "Redpanda",               "url": "https://jobs.ashbyhq.com/redpanda"},
    {"name": "ClickHouse",             "url": "https://job-boards.greenhouse.io/clickhouse"},
    {"name": "QuestDB",                "url": "https://jobs.ashbyhq.com/questdb"},
    {"name": "SingleStore",            "url": "https://job-boards.greenhouse.io/singlestore"},
    {"name": "Yugabyte",               "url": "https://job-boards.greenhouse.io/yugabyte"},
    {"name": "TigerBeetle",            "url": "https://jobs.ashbyhq.com/tigerbeetle"},
    {"name": "Timescale",              "url": "https://job-boards.greenhouse.io/timescale"},

    # ---- AI / ML infra ----
    {"name": "Anyscale",               "url": "https://job-boards.greenhouse.io/anyscale"},
    {"name": "OctoML",                 "url": "https://job-boards.greenhouse.io/octoml"},
    {"name": "Fireworks AI",           "url": "https://jobs.ashbyhq.com/fireworks"},
    {"name": "Baseten",                "url": "https://jobs.ashbyhq.com/baseten"},
    {"name": "Lambda Labs",            "url": "https://job-boards.greenhouse.io/lambdalabs"},
    {"name": "CoreWeave",              "url": "https://job-boards.greenhouse.io/coreweave"},
    {"name": "Crusoe",                 "url": "https://job-boards.greenhouse.io/crusoeenergy"},
    {"name": "Together Compute",       "url": "https://jobs.ashbyhq.com/together"},
    {"name": "Contextual AI",          "url": "https://jobs.ashbyhq.com/contextual"},
    {"name": "Sierra",                 "url": "https://jobs.ashbyhq.com/sierra"},
    {"name": "Decagon",                "url": "https://jobs.ashbyhq.com/decagon"},
    {"name": "Cresta",                 "url": "https://job-boards.greenhouse.io/cresta"},
    {"name": "Writer",                 "url": "https://job-boards.greenhouse.io/writer"},
    {"name": "Cognition AI",           "url": "https://jobs.ashbyhq.com/cognition"},
    {"name": "Imbue",                  "url": "https://jobs.ashbyhq.com/imbue"},
    {"name": "Luma AI",                "url": "https://jobs.ashbyhq.com/lumaai"},
    {"name": "Pika",                   "url": "https://jobs.ashbyhq.com/pika"},
    {"name": "Suno",                   "url": "https://jobs.ashbyhq.com/suno"},
    {"name": "ElevenLabs",             "url": "https://jobs.ashbyhq.com/elevenlabs"},
    {"name": "Hebbia",                 "url": "https://jobs.ashbyhq.com/hebbia"},

    # ---- Fintech / payments (more) ----
    {"name": "Stripe Issuing",         "url": "https://stripe.com/jobs/search"},
    {"name": "Wise",                   "url": "https://job-boards.greenhouse.io/wise"},
    {"name": "Deel",                   "url": "https://job-boards.greenhouse.io/deel"},
    {"name": "Rippling",               "url": "https://www.rippling.com/careers/open-roles"},
    {"name": "Navan",                  "url": "https://job-boards.greenhouse.io/navan"},
    {"name": "Modern Treasury",        "url": "https://job-boards.greenhouse.io/moderntreasury"},
    {"name": "Unit",                   "url": "https://jobs.ashbyhq.com/unit"},
    {"name": "Column",                 "url": "https://jobs.ashbyhq.com/column"},
    {"name": "Increase",               "url": "https://jobs.ashbyhq.com/increase"},
    {"name": "Lithic",                 "url": "https://job-boards.greenhouse.io/lithic"},
    {"name": "Pomelo",                 "url": "https://jobs.ashbyhq.com/pomelo"},
    {"name": "Stytch",                 "url": "https://jobs.ashbyhq.com/stytch"},
    {"name": "Persona",                "url": "https://job-boards.greenhouse.io/persona"},
    {"name": "Alloy",                  "url": "https://job-boards.greenhouse.io/alloy"},
    {"name": "Sardine",                "url": "https://jobs.ashbyhq.com/sardine"},

    # ---- Robotics / autonomy / hardware (more) ----
    {"name": "Physical Intelligence", "url": "https://jobs.ashbyhq.com/physical-intelligence"},
    {"name": "Skild AI",               "url": "https://jobs.ashbyhq.com/skild"},
    {"name": "Bear Robotics",          "url": "https://job-boards.greenhouse.io/bearrobotics"},
    {"name": "Dexterity",              "url": "https://job-boards.greenhouse.io/dexterity"},
    {"name": "Collaborative Robotics", "url": "https://jobs.ashbyhq.com/cobot"},
    {"name": "Bright Machines",        "url": "https://job-boards.greenhouse.io/brightmachines"},
    {"name": "Path Robotics",          "url": "https://job-boards.greenhouse.io/pathrobotics"},
    {"name": "Gecko Robotics",         "url": "https://job-boards.greenhouse.io/geckorobotics"},
    {"name": "Applied Intuition",      "url": "https://job-boards.greenhouse.io/appliedintuition"},
    {"name": "Overland AI",            "url": "https://jobs.ashbyhq.com/overland-ai"},
    {"name": "Wayve",                  "url": "https://job-boards.greenhouse.io/wayve"},
    {"name": "Ghost Autonomy",         "url": "https://job-boards.greenhouse.io/ghostautonomy"},
    {"name": "Vay",                    "url": "https://job-boards.greenhouse.io/vay"},
    {"name": "Stack AV",               "url": "https://jobs.ashbyhq.com/stackav"},
    {"name": "Bot Auto",               "url": "https://jobs.ashbyhq.com/botauto"},
    {"name": "Waabi",                  "url": "https://jobs.lever.co/waabi"},

    # ---- Space / defense tech ----
    {"name": "Vannevar Labs",          "url": "https://jobs.ashbyhq.com/vannevar"},
    {"name": "Applied Physics (API)",  "url": "https://jobs.lever.co/appliedphysics"},
    {"name": "Hadrian",                "url": "https://job-boards.greenhouse.io/hadrian"},
    {"name": "Hermeus",                "url": "https://job-boards.greenhouse.io/hermeus"},
    {"name": "Stoke Space",            "url": "https://job-boards.greenhouse.io/stokespace"},
    {"name": "Varda Space",            "url": "https://job-boards.greenhouse.io/vardaspace"},
    {"name": "K2 Space",               "url": "https://jobs.ashbyhq.com/k2space"},
    {"name": "Albedo",                 "url": "https://jobs.ashbyhq.com/albedo"},
    {"name": "Muon Space",             "url": "https://job-boards.greenhouse.io/muonspace"},
    {"name": "True Anomaly",           "url": "https://jobs.ashbyhq.com/trueanomaly"},
    {"name": "Castelion",              "url": "https://jobs.ashbyhq.com/castelion"},
    {"name": "Epirus",                 "url": "https://job-boards.greenhouse.io/epirus"},
    {"name": "Mach Industries",        "url": "https://jobs.ashbyhq.com/machindustries"},
    {"name": "Chaos Industries",       "url": "https://jobs.ashbyhq.com/chaos"},
    {"name": "Saronic",                "url": "https://jobs.ashbyhq.com/saronic"},

    # ---- More dev tools / consumer ----
    {"name": "Warp",                   "url": "https://jobs.ashbyhq.com/warp"},
    {"name": "Zed",                    "url": "https://jobs.ashbyhq.com/zed"},
    {"name": "Cursor (Anysphere)",     "url": "https://jobs.ashbyhq.com/anysphere"},
    {"name": "Replit",                 "url": "https://job-boards.greenhouse.io/replit"},
    {"name": "Sourcegraph",            "url": "https://job-boards.greenhouse.io/sourcegraph91"},
    {"name": "Codeium",                "url": "https://jobs.ashbyhq.com/codeium"},
    {"name": "Tabnine",                "url": "https://jobs.ashbyhq.com/tabnine"},
    {"name": "Sentry (relay)",         "url": "https://job-boards.greenhouse.io/sentry"},
    {"name": "Render Cloud",           "url": "https://jobs.ashbyhq.com/render"},
    {"name": "Fly.io",                 "url": "https://jobs.ashbyhq.com/flyio"},
    {"name": "Railway",                "url": "https://jobs.ashbyhq.com/railway"},
    {"name": "Coder",                  "url": "https://job-boards.greenhouse.io/coder"},
    {"name": "Gitpod",                 "url": "https://job-boards.greenhouse.io/gitpod"},
    {"name": "Sourcegraph Cody",       "url": "https://job-boards.greenhouse.io/sourcegraph91"},
    {"name": "Mintlify",               "url": "https://jobs.ashbyhq.com/mintlify"},
]

# ================ JOB-TYPE FILTER (systems / OS / embedded / platform) ================

ROLE_ANCHORS = [
    r"\bengineer\b", r"\bengineering\b",
    r"\bdeveloper\b",
    r"\bswe\b", r"\bsde\b",
    r"\bprogrammer\b",
    r"\barchitect\b",
    r"\btech(nical)? lead\b",
]
ROLE_PATTERN = re.compile("|".join(ROLE_ANCHORS), re.IGNORECASE)

DOMAIN_KEYWORDS = [
    # OS / kernel
    r"\boperating system\b", r"\bos\s*(software|engineer|engineering)",
    r"\bkernel\b", r"\blinux\b", r"\bunix\b", r"\bbsd\b",
    r"\bandroid\b(?!\s+app)",
    r"\bsystem(s)? software\b", r"\bsystems? engineer", r"\bsystems? programming\b",
    r"\bdriver(s)?\b", r"\bdevice driver",
    # Embedded / firmware / low-level
    r"\bembedded\b", r"\bfirmware\b", r"\bbootloader\b", r"\bbare[- ]?metal\b",
    r"\brtos\b", r"\bfreertos\b", r"\bzephyr\b",
    r"\bmicrocontroller\b", r"\bmcu\b", r"\bsoc\b", r"\bfpga\b",
    # --- Board / BSP / bring-up (NXP i.MX, Renesas R-Car, etc.) ---
    r"\bbsp\b", r"\bboard support\b", r"\bboard bring[- ]?up\b",
    r"\bbring[- ]?up\b", r"\bsilicon\b", r"\bsilicon bring[- ]?up\b",
    r"\bsoc software\b", r"\byocto\b", r"\bbitbake\b", r"\bu-?boot\b",
    r"\bdevice tree\b", r"\bdevicetree\b", r"\bboard software\b",
    r"\bplatform bring[- ]?up\b",
    # --- Boot / security (BL2/FIP secure boot experience) ---
    r"\bsecure boot\b", r"\bboot\s*(engineer|software|firmware|loader)\b",
    r"\btrusted firmware\b", r"\btf-?a\b",
    # Platform / infra
    r"\bplatform engineer", r"\bplatform software\b", r"\bplatform team\b",
    r"\bplatform sw\b", r"\binfrastructure engineer",
    r"\bcompiler\b", r"\btoolchain\b", r"\bllvm\b", r"\bgcc\b",
    # --- Virtualization / hypervisors ---
    r"\bvirtualization\b", r"\bhypervisor\b", r"\bcontainer runtime\b",
    r"\bqemu\b", r"\bkvm\b", r"\bvirtual machine\b", r"\bemulation\b",
    # Graphics / display / compositor
    r"\bgraphics\b", r"\bgpu\b", r"\bwayland\b", r"\bweston\b",
    r"\bcompositor\b", r"\bdisplay\b", r"\bopengl\b", r"\bvulkan\b",
    r"\brendering\b", r"\bdrm\b", r"\bkms\b", r"\bdrm/kms\b",
    # Performance / reliability
    r"\bperformance engineer", r"\bperformance optimization\b",
    r"\blow[- ]level\b", r"\bsystems? performance\b",
    r"\bsre\b", r"\bsite reliability\b",
    # --- Backend (Kafka, Spring Boot, gRPC, distributed systems) ---
    r"\bbackend engineer", r"\bback[- ]end engineer", r"\bback[- ]end software\b",
    r"\bdistributed systems\b", r"\bkafka\b", r"\bspring boot\b", r"\bgrpc\b",
    r"\bdata pipeline\b", r"\bstreaming\b(?!\s+media)",
    # Languages typical at this layer
    r"\brust\b", r"\bc\+\+\b", r"(?<![a-z])\bc\b(?![a-z+#])",
    r"\bgolang\b", r"\bgo developer\b", r"\bgo engineer\b",
]
DOMAIN_PATTERN = re.compile("|".join(DOMAIN_KEYWORDS), re.IGNORECASE)

EXCLUDE_KEYWORDS = [
    r"\brecruiter\b", r"\bsales\b", r"\bmarketing\b",
    r"\bsupport engineer\b",
    r"\bbusiness development\b", r"\bhr\b", r"\bpeople ops\b",
    r"\bfinance\b", r"\baccountant\b", r"\blegal\b",
    r"\bcustomer success\b", r"\baccount manager\b",
    r"\bproduct manager\b", r"\bprogram manager\b", r"\bproject manager\b",
    r"\bdesigner\b", r"\bux\b", r"\bui designer\b",
    r"\bdata analyst\b", r"\bbusiness analyst\b",
    r"\bfrontend\b", r"\bfront[- ]end\b", r"\bweb developer\b",
]
EXCLUDE_PATTERN = re.compile("|".join(EXCLUDE_KEYWORDS), re.IGNORECASE)

# ================ USA-ONLY LOCATION FILTER ================
# Looks at the parent element of each job link for location text.
# Loose mode: include jobs where no location text is found.

US_INDICATORS = [
    r"\bunited states\b", r"\busa?\b", r"\bu\.s\.a?\.?\b",
    r"\bremote\s*[-–—]?\s*us\b", r"\bus\s*remote\b", r"\bremote\s*\(us\)",
    # State names
    r"\balabama\b", r"\balaska\b", r"\barizona\b", r"\barkansas\b", r"\bcalifornia\b",
    r"\bcolorado\b", r"\bconnecticut\b", r"\bdelaware\b", r"\bflorida\b", r"\bgeorgia\b",
    r"\bhawaii\b", r"\bidaho\b", r"\billinois\b", r"\bindiana\b", r"\biowa\b",
    r"\bkansas\b", r"\bkentucky\b", r"\blouisiana\b", r"\bmaine\b", r"\bmaryland\b",
    r"\bmassachusetts\b", r"\bmichigan\b", r"\bminnesota\b", r"\bmississippi\b", r"\bmissouri\b",
    r"\bmontana\b", r"\bnebraska\b", r"\bnevada\b", r"\bnew hampshire\b", r"\bnew jersey\b",
    r"\bnew mexico\b", r"\bnew york\b", r"\bnorth carolina\b", r"\bnorth dakota\b", r"\bohio\b",
    r"\boklahoma\b", r"\boregon\b", r"\bpennsylvania\b", r"\brhode island\b", r"\bsouth carolina\b",
    r"\bsouth dakota\b", r"\btennessee\b", r"\btexas\b", r"\butah\b", r"\bvermont\b",
    r"\bvirginia\b", r"\bwashington\b", r"\bwest virginia\b", r"\bwisconsin\b", r"\bwyoming\b",
    # State abbreviations (comma-prefixed to reduce false positives)
    r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|"
    r"mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)\b",
    # Common US cities
    r"\bsan francisco\b", r"\bnew york\b", r"\bseattle\b", r"\bboston\b", r"\baustin\b",
    r"\blos angeles\b", r"\bchicago\b", r"\bdenver\b", r"\bsanta clara\b", r"\bsunnyvale\b",
    r"\bmountain view\b", r"\bpalo alto\b", r"\bcupertino\b", r"\bredmond\b", r"\bbellevue\b",
    r"\bsan jose\b", r"\bsan diego\b", r"\bphiladelphia\b", r"\bdetroit\b", r"\batlanta\b",
    r"\bmiami\b", r"\bdallas\b", r"\bhouston\b", r"\bphoenix\b", r"\blas vegas\b",
    r"\bportland\b", r"\bnashville\b", r"\bwashington,?\s*dc\b",
]
US_PATTERN = re.compile("|".join(US_INDICATORS), re.IGNORECASE)

# Non-US indicators — if any of these match, exclude.
NON_US_INDICATORS = [
    r"\bindia\b", r"\bbengaluru\b", r"\bbangalore\b", r"\bhyderabad\b", r"\bpune\b",
    r"\bchennai\b", r"\bgurgaon\b", r"\bnoida\b", r"\bmumbai\b", r"\bdelhi\b",
    r"\bunited kingdom\b", r"\buk\b", r"\blondon\b", r"\bcambridge,?\s*uk\b",
    r"\bgermany\b", r"\bberlin\b", r"\bmunich\b", r"\bfrance\b", r"\bparis\b",
    r"\bireland\b", r"\bdublin\b", r"\bnetherlands\b", r"\bamsterdam\b",
    r"\bspain\b", r"\bbarcelona\b", r"\bmadrid\b", r"\bitaly\b", r"\bswitzerland\b",
    r"\bcanada\b", r"\btoronto\b", r"\bvancouver\b", r"\bmontreal\b",
    r"\bsingapore\b", r"\bjapan\b", r"\btokyo\b", r"\bchina\b", r"\bshanghai\b",
    r"\bbeijing\b", r"\bshenzhen\b", r"\btaiwan\b", r"\btaipei\b",
    r"\baustralia\b", r"\bsydney\b", r"\bmelbourne\b", r"\bbrazil\b", r"\bmexico\b",
    r"\bisrael\b", r"\btel aviv\b", r"\bhaifa\b", r"\bpoland\b", r"\bukraine\b",
    r"\bromania\b", r"\bczech\b", r"\bportugal\b",
    r"\bemea\b", r"\bapac\b", r"\blatam\b",
]
NON_US_PATTERN = re.compile("|".join(NON_US_INDICATORS), re.IGNORECASE)


def is_target_job(title):
    if EXCLUDE_PATTERN.search(title):
        return False
    if not ROLE_PATTERN.search(title):
        return False
    # Entry-level pass-through: generic new-grad/junior/intern SWE titles
    # are kept even without a domain keyword in the title, because the
    # systems detail is usually in the description, not the title itself.
    if ENTRY_PATTERN.search(title):
        return True
    # General software-engineering pass-through: backend/microservices/SWE
    # titles also qualify, not only the explicitly low-level ones.
    if GENERAL_SWE_PATTERN.search(title):
        return True
    if not DOMAIN_PATTERN.search(title):
        return False
    return True


# Entry-level signals — if a title is clearly junior AND has a role anchor,
# let it through regardless of domain keywords.
ENTRY_PATTERN = re.compile(
    r"\bnew[- ]grad(uate)?\b|\bentry[- ]level\b|\bjunior\b|\bjr\.?\b|"
    r"\bassociate (software |systems )?engineer\b|\bgraduate (software )?engineer\b|"
    r"\bintern(ship)?\b|\bco[- ]?op\b|\bengineer\s+(i|1)\b|\bearly career\b|"
    r"\bsoftware engineer\s*[-,]?\s*(new grad|university|campus|2025|2026)\b|"
    r"\buniversity grad",
    re.IGNORECASE,
)

# General software-engineering titles that qualify on their own
# (backend, microservices, fullstack) even when no low-level keyword appears.
GENERAL_SWE_PATTERN = re.compile(
    r"\bsoftware (engineer|developer)\b|\bsoftware development engineer\b|"
    r"\bswe\b|\bsde\b|\bfull[- ]?stack\b|\bback[- ]?end\b|"
    r"\bservices? engineer\b|\bapplication(s)? (engineer|developer)\b|"
    r"\bapi (engineer|developer)\b|\bmicroservices?\b|\bjava (engineer|developer)\b|"
    r"\b\.net (engineer|developer)\b|\.net developer|c# developer|"
    r"\bpython (engineer|developer)\b|"
    r"\bspring boot\b|\bkafka\b|\bgrpc\b|\bdistributed systems\b",
    re.IGNORECASE,
)


def is_us_location(location_text):
    """Practical filter: exclude only if a clearly non-US indicator appears.

    Why not require a positive US match? Because most career sites don't put
    location text where we can reliably find it — it's inside JS-rendered
    cards, sibling nodes, or images. Demanding a positive US match drops
    95% of legitimate US jobs. Excluding obvious non-US locations keeps the
    signal high without that loss. Some non-US jobs will slip through on
    sites where the location isn't near the link; that's the honest tradeoff."""
    if not location_text:
        return True  # unknown location = include
    return not bool(NON_US_PATTERN.search(location_text))


# ================ SENIORITY / EXPERIENCE LEVEL FROM JOB TITLE ================
# Returns a label like "Intern", "Entry / New Grad", "Mid (~2-5y)", "Senior (~5-8y)",
# "Staff (~8-12y)", "Principal (~12+y)", or "" if no clear level signal in title.
# Years are rough industry estimates — actual requirements vary by company.

_SENIORITY_PATTERNS = [
    # Internships / new grad first (most specific)
    ("Intern", r"\bintern(ship)?\b|\bco[- ]?op\b"),
    ("Entry / New Grad", r"\bnew[- ]grad(uate)?\b|\bentry[- ]level\b|\bjunior\b|\bjr\.?\b|\bassociate\b|\bgraduate engineer\b"),
    # Senior-most labels (specific terms beat generic "engineer")
    ("Fellow", r"\bfellow\b"),
    ("Distinguished (~15+y)", r"\bdistinguished\b"),
    ("Principal (~12+y)", r"\bprincipal\b"),
    ("Senior Staff (~10-15y)", r"\bsr\.?\s*staff\b|\bsenior\s+staff\b"),
    ("Staff (~8-12y)", r"\bstaff\b"),
    ("Senior (~5-8y)", r"\bsenior\b|\bsr\.?\b|\bsnr\b"),
    ("Lead (~7-10y)", r"\blead\b(?!\s+generation)"),
    # Levels like "Engineer II" or "Engineer 3" (common at big tech)
    ("Mid (~2-5y)", r"\bengineer\s+(ii|2|iii|3)\b|\blevel\s*(2|3)\b|\bl[345]\b"),
    ("Entry (~0-2y)", r"\bengineer\s+(i|1)\b|\blevel\s*1\b|\bl[12]\b"),
]
_SENIORITY_COMPILED = [(label, re.compile(pat, re.IGNORECASE)) for label, pat in _SENIORITY_PATTERNS]


def detect_seniority(title):
    """Return a seniority label from a job title, or '' if unclear."""
    if not title:
        return ""
    # First match wins; ordering is intentional (specific before generic).
    for label, pattern in _SENIORITY_COMPILED:
        if pattern.search(title):
            return label
    # Bare "Software Engineer" / "Developer" with no other qualifier → likely mid
    if re.search(r"\b(software\s+)?(engineer|developer|programmer)\b", title, re.IGNORECASE):
        return "Mid (~2-5y)"
    return ""


# ================ JOB-LINK DETECTION HEURISTICS ================

JOB_URL_PATTERNS = [
    r"/jobs?/", r"/careers?/", r"/positions?/", r"/openings?/",
    r"/roles?/", r"/vacancy/", r"/vacancies/", r"/apply/",
    r"/job-detail", r"/job/",
    r"greenhouse\.io/.+/jobs/\d+",
    r"lever\.co/.+/[a-f0-9-]{20,}",
    r"ashbyhq\.com/.+/.+",
    r"workday\.com/.+/job/", r"myworkdayjobs\.com/.+/job/",
    r"smartrecruiters\.com/.+/\d+",
    r"icims\.com/jobs/\d+",
    r"dejobs\.org/.+/job/",
]

NON_JOB_TEXT = {
    "careers", "jobs", "open positions", "all jobs", "view all",
    "see all", "apply", "login", "sign in", "search", "filter",
    "home", "about", "contact", "privacy", "terms", "back",
    "next", "previous", "more", "load more",
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (job_id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def looks_like_job_link(href, text, base_url):
    if not href or not text:
        return False
    text_clean = text.strip()
    if len(text_clean) < 3 or len(text_clean) > 200:
        return False
    if text_clean.lower() in NON_JOB_TEXT:
        return False
    full_url = urljoin(base_url, href)
    if not any(re.search(p, full_url, re.IGNORECASE) for p in JOB_URL_PATTERNS):
        return False
    if not re.match(r"^[A-Za-z0-9]", text_clean):
        return False
    return True


def get_nearby_location(a_tag):
    """Look for location text in the link's own immediate context.

    Strategy:
      1. Check next siblings of the <a> (location is often the next line/element).
      2. Check the immediate parent's own text (excluding nested links/headings).
      3. Check the immediate parent's next sibling.
    Stops at the first short text fragment containing a US or non-US indicator.
    Deliberately does NOT climb high in the tree — that grabs neighbors' data.
    """
    def looks_like_location(text):
        if not text or len(text) > 200:
            return False
        return bool(US_PATTERN.search(text) or NON_US_PATTERN.search(text))

    # 1. Next siblings of the link
    for sib in a_tag.next_siblings:
        if hasattr(sib, "get_text"):
            t = sib.get_text(" ", strip=True)
        else:
            t = str(sib).strip()
        if t and looks_like_location(t):
            return t

    # 2. Parent's own text minus the link's text
    parent = a_tag.parent
    if parent is not None:
        full = parent.get_text(" ", strip=True)
        link_text = a_tag.get_text(" ", strip=True)
        remainder = full.replace(link_text, "", 1).strip()
        if looks_like_location(remainder):
            return remainder

        # 3. Parent's next sibling (e.g. <li><a>title</a></li><li>location</li>)
        for sib in parent.next_siblings:
            if hasattr(sib, "get_text"):
                t = sib.get_text(" ", strip=True)
            else:
                t = str(sib).strip()
            if t and looks_like_location(t):
                return t

    return ""


def extract_jobs(html, base_url, selector=None):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen_urls = set()

    if selector:
        elements = soup.select(selector)
        for el in elements:
            a = el if el.name == "a" else el.find("a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            title = el.get_text(strip=True) if el.name != "a" else a.get_text(strip=True)
            full_url = urljoin(base_url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            jobs.append({"title": title, "url": full_url, "location": get_nearby_location(a)})
    else:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not looks_like_job_link(href, text, base_url):
                continue
            full_url = urljoin(base_url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            jobs.append({"title": text, "url": full_url, "location": get_nearby_location(a)})

    return jobs


async def fetch_page(browser, url):
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    )
    await _apply_stealth(context)
    page = await context.new_page()
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        # Give the page a moment to render JS — networkidle never fires on heavy SPAs.
        await page.wait_for_timeout(4000)
        return await page.content()
    finally:
        await context.close()


# ================ ATS-SPECIFIC FETCHERS ================
# These hit the JSON APIs that career-page widgets call internally. Much
# faster and more reliable than scraping rendered HTML.

WORKDAY_RE = re.compile(r"https?://([^.]+)\.[^/]*myworkdayjobs\.com/(?:[a-z-]+/)?([^/?#]+)", re.I)


def detect_ats(url):
    """Return 'workday' | 'greenhouse' | 'lever' | 'ashby' | 'oracle' | None"""
    u = url.lower()
    if "myworkdayjobs.com" in u or "workday.com" in u:
        return "workday"
    if "greenhouse.io" in u or "boards.greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "oraclecloud.com" in u and "candidateexperience" in u:
        return "oracle"
    return None


def fetch_workday_api(url):
    """Workday's job board renders client-side from POSTing to /wday/cxs/{tenant}/{site}/jobs.
    Returns list of {title, url, location} dicts, or raises."""
    m = WORKDAY_RE.search(url)
    if not m:
        raise ValueError("Couldn't parse Workday tenant/site from URL")
    tenant, site = m.group(1), m.group(2)

    # The host pattern is e.g. nvidia.wd5.myworkdayjobs.com — need full host.
    host = urlparse(url).netloc
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    # Detect the locale prefix from the original URL if present (e.g. /en-US/),
    # otherwise default to /en-US/. Workday public job URLs require this segment.
    locale_match = re.search(r"https?://[^/]+/([a-z]{2}-[A-Z]{2})/", url)
    locale = locale_match.group(1) if locale_match else "en-US"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    all_jobs = []
    offset = 0
    while True:
        r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                          headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            ext = p.get("externalPath", "")
            if ext:
                # Workday public URL format: https://{host}/{locale}/{site}{externalPath}
                # externalPath is like "/job/Santa-Clara/Senior-Engineer_JR12345"
                job_url = f"https://{host}/{locale}/{site}{ext}"
            else:
                job_url = url
            all_jobs.append({
                "title": p.get("title", "").strip(),
                "url": job_url,
                "location": p.get("locationsText", "").strip(),
            })
        total = data.get("total", 0)
        offset += 20
        if offset >= total or offset > 2000:  # safety cap
            break
    return all_jobs


def fetch_greenhouse_api(url):
    """Pull jobs from boards-api.greenhouse.io. Handles both boards.greenhouse.io
    and embedded greenhouse iframes."""
    # Try to find the company slug
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", url, re.I)
    if not m:
        raise ValueError("Couldn't parse Greenhouse company slug")
    slug = m.group(1)
    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(api, timeout=20)
    r.raise_for_status()
    return [
        {
            "title": j.get("title", "").strip(),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
        }
        for j in r.json().get("jobs", [])
    ]


def fetch_lever_api(url):
    m = re.search(r"lever\.co/([a-z0-9_-]+)", url, re.I)
    if not m:
        raise ValueError("Couldn't parse Lever company slug")
    slug = m.group(1)
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(api, timeout=20)
    r.raise_for_status()
    return [
        {
            "title": j.get("text", "").strip(),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
        }
        for j in r.json()
    ]


def fetch_ashby_api(url):
    m = re.search(r"ashbyhq\.com/([a-z0-9_-]+)", url, re.I)
    if not m:
        raise ValueError("Couldn't parse Ashby company slug")
    slug = m.group(1)
    api = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(api, timeout=20)
    r.raise_for_status()
    return [
        {
            "title": j.get("title", "").strip(),
            "url": j.get("jobUrl", ""),
            "location": j.get("location", ""),
        }
        for j in r.json().get("jobs", [])
    ]


def fetch_oracle_api(url):
    """Oracle Cloud HCM exposes REST API at /hcmRestApi/resources/latest/recruitingCEJobRequisitions.
    Used by Oracle itself, Texas Instruments, and many other large companies."""
    # Extract the tenant host (e.g. edbz.fa.us2.oraclecloud.com)
    parsed = urlparse(url)
    host = parsed.netloc
    if not host:
        raise ValueError("Couldn't parse Oracle host")

    headers = {
        "Accept": "application/json",
        "REST-Framework-Version": "7",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    all_jobs = []
    offset = 0
    limit = 200
    while True:
        api = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations,flexFieldsFacet.values"
            f"&finder=findReqs;siteNumber=CX_1,facetsList=LOCATIONS%3BWORK_LOCATIONS%3BTITLES,"
            f"limit={limit},offset={offset}"
        )
        r = requests.get(api, headers=headers, timeout=25)
        r.raise_for_status()
        data = r.json()
        items = (data.get("items") or [{}])[0].get("requisitionList", [])
        if not items:
            break
        for j in items:
            job_id = j.get("Id", "")
            locs = j.get("PrimaryLocation", "") or ""
            if j.get("secondaryLocations"):
                extras = [s.get("Name", "") for s in j["secondaryLocations"]]
                if extras:
                    locs = locs + " | " + ", ".join(extras)
            # Build the public job URL — pattern varies, but this works for most tenants:
            job_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/CX/job/{job_id}"
            all_jobs.append({
                "title": j.get("Title", "").strip(),
                "url": job_url,
                "location": locs,
            })
        offset += limit
        if offset > 5000:  # safety cap
            break
        if len(items) < limit:
            break
    return all_jobs


def fetch_simplify_newgrad(url):
    """SimplifyJobs/New-Grad-Positions auto-updates a listings.json every ~30 min
    with new-grad SWE/Quant/PM roles (US/Canada/Remote). This is a clean
    structured feed — far better than scraping the README. We try the known
    raw.githubusercontent paths for the listings file."""
    candidates = [
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/main/.github/scripts/listings.json",
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    data = None
    for c in candidates:
        try:
            r = requests.get(c, headers=headers, timeout=20)
            if r.status_code == 200 and r.text.strip().startswith("["):
                data = r.json()
                break
        except Exception:
            continue
    if data is None:
        raise ValueError("Could not fetch SimplifyJobs listings.json")

    jobs = []
    for item in data:
        # Schema: title, company_name, locations[], url, active, date_updated, visible
        if not item.get("active", True):
            continue
        if item.get("visible") is False:
            continue
        title = item.get("title", "").strip()
        company = item.get("company_name", "").strip()
        locs = item.get("locations", []) or []
        loc_str = ", ".join(locs) if isinstance(locs, list) else str(locs)
        link = item.get("url", "")
        if not title or not link:
            continue
        jobs.append({
            "title": f"{title}  —  ({company})",
            "url": link,
            "location": loc_str,
        })
    return jobs


def fetch_via_ats(url):
    """Try ATS API. Returns (jobs, method_name) on success, or (None, None) if not applicable."""
    # SimplifyJobs new-grad aggregator — special-cased.
    if "github.com/SimplifyJobs/New-Grad-Positions" in url or "simplifyjobs" in url.lower():
        try:
            return fetch_simplify_newgrad(url), "simplify-newgrad"
        except Exception:
            return None, None

    ats = detect_ats(url)
    try:
        if ats == "workday":
            return fetch_workday_api(url), "workday-api"
        if ats == "greenhouse":
            return fetch_greenhouse_api(url), "greenhouse-api"
        if ats == "lever":
            return fetch_lever_api(url), "lever-api"
        if ats == "ashby":
            return fetch_ashby_api(url), "ashby-api"
        if ats == "oracle":
            return fetch_oracle_api(url), "oracle-api"
    except Exception:
        return None, None  # Fall through to browser scraping
    return None, None


def _format_text_attachment(new_jobs):
    """Plain text, grouped by company, columns aligned. Most modern email
    clients auto-detect URLs and make them clickable when opening the .txt."""
    from collections import defaultdict
    grouped = defaultdict(list)
    for company, j in new_jobs:
        grouped[company].append(j)

    lines = [
        "=" * 78,
        f"  {len(new_jobs)} new software / systems job(s)",
        f"  Generated: {datetime.now().strftime('%A, %b %d %Y at %H:%M')}",
        "=" * 78,
        "",
    ]
    for company in sorted(grouped.keys()):
        jobs = grouped[company]
        lines.append(f"### {company}  ({len(jobs)} role{'s' if len(jobs) != 1 else ''})")
        lines.append("-" * 78)
        for j in jobs:
            lines.append(f"  • {j['title']}")
            level = detect_seniority(j['title'])
            if level:
                lines.append(f"      Level:    {level}")
            if j.get("location"):
                loc = j["location"][:100]
                lines.append(f"      Location: {loc}")
            lines.append(f"      Link:     {j['url']}")
            lines.append("")
        lines.append("")
    return "\n".join(lines)


def _format_html_attachment(new_jobs):
    """HTML version — fully clickable links, looks like a clean job list."""
    from collections import defaultdict
    grouped = defaultdict(list)
    for company, j in new_jobs:
        grouped[company].append(j)

    parts = ["""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>New Jobs</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 800px; margin: 24px auto; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; border-bottom: 2px solid #444; padding-bottom: 8px; }
  h2 { font-size: 16px; color: #0066cc; margin-top: 24px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
  .job { margin: 10px 0 14px 0; padding-left: 14px; border-left: 3px solid #e0e0e0; }
  .title { font-weight: 600; font-size: 14px; }
  .level { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 10px;
           background: #eef4ff; color: #2563eb; margin-left: 8px; vertical-align: middle; }
  .loc { color: #666; font-size: 13px; margin-top: 2px; }
  a { color: #0066cc; text-decoration: none; word-break: break-all; }
  a:hover { text-decoration: underline; }
  .meta { color: #888; font-size: 12px; margin-bottom: 16px; }
</style></head><body>"""]

    parts.append(f"<h1>{len(new_jobs)} new software / systems job(s)</h1>")
    parts.append(f'<div class="meta">{datetime.now().strftime("%A, %b %d %Y at %H:%M")}</div>')

    for company in sorted(grouped.keys()):
        jobs = grouped[company]
        parts.append(f'<h2>{company} <span style="font-weight:normal;color:#888;">({len(jobs)} role{"s" if len(jobs) != 1 else ""})</span></h2>')
        for j in jobs:
            title = (j["title"] or "Untitled").replace("<", "&lt;").replace(">", "&gt;")
            loc = (j.get("location") or "").replace("<", "&lt;").replace(">", "&gt;")
            level = detect_seniority(j["title"])
            url = j["url"]
            parts.append('<div class="job">')
            level_html = f'<span class="level">{level}</span>' if level else ''
            parts.append(f'  <div class="title">{title}{level_html}</div>')
            if loc:
                parts.append(f'  <div class="loc">📍 {loc[:120]}</div>')
            parts.append(f'  <div><a href="{url}">{url}</a></div>')
            parts.append('</div>')

    parts.append("</body></html>")
    return "\n".join(parts)


def send_email(new_jobs):
    timestamp = datetime.now().strftime("%b %d %H:%M")

    # Build a clean HTML body that renders inline in the email (clickable!).
    html_body = _format_html_attachment(new_jobs)

    # Also include a plain-text fallback for email clients that don't render HTML.
    text_body = _format_text_attachment(new_jobs)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"{len(new_jobs)} new systems job(s) — {timestamp}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    # The body alternative (text + HTML) — Gmail will show the HTML.
    body_alt = MIMEMultipart("alternative")
    body_alt.attach(MIMEText(text_body, "plain", "utf-8"))
    body_alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body_alt)

    if ATTACH_TXT_FILE:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")

        # Formatted plain-text file
        txt_attach = MIMEApplication(text_body.encode("utf-8"), _subtype="txt")
        txt_attach.add_header("Content-Disposition", "attachment",
                              filename=f"jobs_{stamp}.txt")
        msg.attach(txt_attach)

        # HTML file with guaranteed-clickable links (open in browser)
        html_attach = MIMEApplication(html_body.encode("utf-8"), _subtype="html")
        html_attach.add_header("Content-Disposition", "attachment",
                               filename=f"jobs_{stamp}.html")
        msg.attach(html_attach)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


async def process_company(browser, company, sem):
    async with sem:
        name = company["name"]
        url = company["url"]
        selector = company.get("selector")

        # Try ATS-specific API first (fast, reliable, structured data).
        ats_jobs, method = await asyncio.to_thread(fetch_via_ats, url)
        if ats_jobs is not None:
            return name, ats_jobs, None, method

        # Known bot-walled / JS-fortress sites: scraping them always returns 0
        # AND each one hangs for the full timeout, wrecking cycle time. Skip them
        # entirely — use native job alerts on these companies' own sites instead.
        BOT_WALLED = {
            "Google", "Apple", "Meta", "Microsoft", "Amazon", "Netflix", "IBM",
            "Oracle", "LinkedIn", "Pinterest", "ByteDance / TikTok US", "Niantic",
            "Twitch", "AT&T", "T-Mobile", "L3Harris", "Tesla", "Snowflake",
            "Cohere", "InfluxData", "MemryX", "Rivos",
        }
        if name in BOT_WALLED:
            return name, [], None, "skipped (bot-walled)"

        # Fall back to browser scraping with heuristics.
        try:
            html = await fetch_page(browser, url)
            jobs = extract_jobs(html, url, selector)
            return name, jobs, None, "scrape"
        except Exception as e:
            return name, [], str(e)[:120], "scrape"


async def verify_url(url, session_lock):
    """Quickly check if a URL is reachable.
    Returns True if 200/3xx (or rate-limited/forbidden — we don't drop those).
    Returns False only on clear 404/410/etc. or unreachable host."""
    def _check():
        try:
            # Use a real-browser User-Agent so we don't get blocked.
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            }
            # HEAD first (cheap). Some sites block HEAD; fall back to a small GET.
            r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
            if r.status_code == 405 or r.status_code == 403:
                # Method not allowed or forbidden → try GET with range header
                headers["Range"] = "bytes=0-1023"
                r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            if 200 <= r.status_code < 300:
                return True
            if 300 <= r.status_code < 400:
                # Redirect — usually followed automatically by requests, but be defensive.
                return True
            if r.status_code in (403, 429, 503):
                # Rate-limited or temporarily blocked — don't drop the job.
                return True
            return False  # 404, 410, 500, etc.
        except requests.RequestException:
            # Network error → don't drop; might be transient.
            return True

    return await asyncio.to_thread(_check)


async def verify_urls(jobs):
    """Verify a list of (company_name, job_dict) tuples in parallel.
    Returns (verified_jobs, dropped_count)."""
    if not jobs:
        return jobs, 0

    sem = asyncio.Semaphore(URL_VERIFY_CONCURRENCY)

    async def _verify_one(item):
        async with sem:
            return await verify_url(item[1]["url"], sem)

    results = await asyncio.gather(*[_verify_one(item) for item in jobs])
    verified = [job for job, ok in zip(jobs, results) if ok]
    dropped = len(jobs) - len(verified)
    return verified, dropped


async def check_once(conn, browser):
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [process_company(browser, c, sem) for c in COMPANIES]
    results = await asyncio.gather(*tasks)

    cur = conn.cursor()
    new_jobs = []
    candidates = []  # jobs that pass filters and aren't yet in DB; verified before commit

    for name, jobs, err, method in results:
        if err:
            print(f"  ! {name}: {err}")
            continue
        if method == "skipped (bot-walled)":
            continue  # silently skip — use native alerts for these

        matched = 0
        us_pass = 0
        cand_count = 0
        for j in jobs:
            if not is_target_job(j["title"]):
                continue
            matched += 1
            if not is_us_location(j.get("location", "")):
                continue
            us_pass += 1
            job_id = j["url"]
            cur.execute("SELECT 1 FROM seen WHERE job_id = ?", (job_id,))
            if cur.fetchone():
                continue
            candidates.append((name, j))
            cand_count += 1

        tag = f" [{method}]" if method and method != "scrape" else ""
        print(f"  {name}: {len(jobs)} links | {matched} systems | {us_pass} US | {cand_count} candidate{tag}")

    # Verify candidate URLs are reachable before alerting on them.
    if VERIFY_URLS and candidates:
        print(f"  Verifying {len(candidates)} candidate URL(s)...")
        verified, dropped = await verify_urls(candidates)
        if dropped:
            print(f"  Dropped {dropped} broken URL(s) — kept {len(verified)}.")
        new_jobs = verified
    else:
        new_jobs = candidates

    # Now commit the verified jobs to the database so we don't re-alert next cycle.
    for _, j in new_jobs:
        cur.execute("INSERT OR IGNORE INTO seen (job_id) VALUES (?)", (j["url"],))

    conn.commit()
    return new_jobs


async def main():
    conn = init_db()
    first_run = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0

    print(f"Watching {len(COMPANIES)} companies, every {CHECK_INTERVAL_MINUTES} min.")
    print(f"Concurrency: {CONCURRENCY}. Daily digest at {DAILY_DIGEST_HOUR:02d}:00. Ctrl+C to stop.\n")

    digest_buffer = []          # accumulates all new jobs seen since last digest
    last_digest_date = datetime.now().date()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            while True:
                start = datetime.now()
                print(f"[{start.strftime('%H:%M:%S')}] Cycle starting...")
                new_jobs = await check_once(conn, browser)
                elapsed = (datetime.now() - start).total_seconds()
                print(f"  Cycle done in {elapsed:.0f}s.")

                if first_run:
                    print("  Seeded DB. Future cycles alert only on newly added jobs.\n")
                    first_run = False
                elif new_jobs:
                    print(f"  {len(new_jobs)} new job(s). Emailing...")
                    try:
                        send_email(new_jobs)
                        print("  Email sent.\n")
                    except Exception as e:
                        print(f"  Email failed: {e}\n")
                    digest_buffer.extend(new_jobs)
                else:
                    print("  No new jobs.\n")

                # ---- Daily digest: once per day at DAILY_DIGEST_HOUR ----
                now = datetime.now()
                if (now.hour == DAILY_DIGEST_HOUR
                        and now.date() != last_digest_date):
                    last_digest_date = now.date()
                    if digest_buffer:
                        print(f"  Sending daily digest ({len(digest_buffer)} jobs in last 24h)...")
                        try:
                            send_email(digest_buffer)
                            print("  Digest sent.\n")
                        except Exception as e:
                            print(f"  Digest failed: {e}\n")
                    else:
                        print("  Daily digest: nothing new in the last 24h.\n")
                    digest_buffer = []

                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
