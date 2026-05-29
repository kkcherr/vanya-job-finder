#!/usr/bin/env python3
"""Daily job search: Corporate Development / Finance roles in London."""

import hashlib
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
REED_API_KEY = os.environ.get("REED_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SEEN_JOBS_FILE = "job_search/seen_jobs.json"
MAX_SEEN_JOBS = 2000

# Domains Tavily is restricted to — job boards and career pages only
TAVILY_DOMAINS = [
    "linkedin.com", "indeed.co.uk", "boards.greenhouse.io", "wellfound.com",
    "jobs.lever.co", "workable.com", "smartrecruiters.com",
    "goldmansachs.com", "careers.jpmorgan.com", "morganstanley.com",
    "careers.bankofamerica.com", "jobs.citi.com", "db.com", "ubs.com",
    "lazard.com", "rothschildandco.com", "evercore.com", "moelis.com",
    "jefferies.com", "pwpartners.com", "hl.com", "berenberg.com",
    "hsbc.com", "lloydsbankinggroup.com", "jobs.natwestgroup.com",
    "standardchartered.com", "barclays.com", "investec.com",
    "blackstone.com", "kkr.com", "carlyle.com", "apollo.com",
    "warburgpincus.com", "apax.com", "permira.com", "bridgepoint.eu",
    "generalatlantic.com", "3i.com", "macquarie.com",
    "careers.blackrock.com", "schroders.com", "man.com",
    "nomura.com", "mizuhogroup.com", "smbcgroup.com",
    "careers.google.com", "metacareers.com", "careers.microsoft.com",
    "amazon.jobs", "databricks.com", "palantir.com",
    "jobs.nvidia.com", "careers.arm.com", "careers.amd.com",
    "revolut.com", "starlingbank.com", "wise.com",
    "careers.equinix.com", "careers.digitalrealty.com",
    "reed.co.uk", "totaljobs.com", "cityfalcon.com",
    "efinancialcareers.com", "efinancialcareers.co.uk",
]

# Companies known to use Greenhouse — corp dev, finance, and tech firms in London
GREENHOUSE_COMPANIES = [
    "stripe", "palantir", "figma", "checkout",
    "revolut", "wise", "monzo", "starling",
    "lazard", "jefferies", "evercore", "greenhill",
    "bridgepoint", "cinven", "apax", "permira",
    "deliveroo", "skyscanner", "transferwise",
    "form3", "oaknorth", "zilch", "cleo",
    "marshmallow", "tractable", "quantexa",
]

EXACT_KEYWORDS = ["corporate development associate"]
WIDE_KEYWORDS = [
    "investment banking", "structured finance",
    "capital markets", "corporate finance", "m&a",
]

# Job title must contain at least one of these specific finance phrases
TITLE_MUST_CONTAIN = [
    "corporate development",
    "investment banking",
    "investment bank",
    "structured finance",
    "capital markets",
    "leveraged finance",
    "debt capital",
    "equity capital",
    "m&a",
    "mergers and acquisitions",
    "mergers & acquisitions",
    "private equity",
    "corporate finance",
    "financial sponsor",
    "deal origination",
    "lbo",
    "leveraged buyout",
]

DAYS_LOOKBACK = 2  # covers full previous calendar day regardless of run time

PRIORITY_COMPANIES = [
    "mufg", "mitsubishi ufj",
    "smbc", "sumitomo mitsui",
    "mizuho",
    "credit agricole", "crédit agricole", "cacib",
    "bnp paribas", "bnp",
    "société générale", "societe generale", "socgen",
    "goldman sachs", "goldman",
    "bank of america", "bofa", "merrill lynch",
    "citi", "citibank", "citigroup",
    "jpmorgan", "jp morgan", "j.p. morgan",
    "ing",
    "rabobank",
    "abn amro", "abn-amro",
    "santander",
    "deutsche bank",
    "intesa sanpaolo", "intesa",
    "bbva",
    "macquarie",
    "monzo",
    "revolut",
]

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    search_type: str

    @property
    def job_id(self) -> str:
        return hashlib.md5(self.url.encode()).hexdigest()

    @property
    def is_priority(self) -> bool:
        c = self.company.lower()
        return any(p in c for p in PRIORITY_COMPANIES)

    def telegram_text(self) -> str:
        label = f"{self.source} · {self.search_type}"
        prefix = "PRIORITY | " if self.is_priority else ""
        title = _esc(f"{prefix}{self.title}")
        company = _esc(self.company)
        location = _esc(self.location)
        label = _esc(label)
        return (
            f"*{title}*\n"
            f"Company: {company}\n"
            f"Location: {location}\n"
            f"Source: {label}\n"
            f"[View Job]({self.url})"
        )


def _esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def load_seen_jobs() -> set:
    if not os.path.exists(SEEN_JOBS_FILE):
        return set()
    with open(SEEN_JOBS_FILE) as f:
        return set(json.load(f).get("job_ids", []))


def save_seen_jobs(seen: set) -> None:
    job_list = list(seen)[-MAX_SEEN_JOBS:]
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump({"job_ids": job_list}, f, indent=2)


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


def _title_is_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TITLE_MUST_CONTAIN)


def search_reed(query: str, search_type: str) -> list:
    if not REED_API_KEY:
        return []
    jobs = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)
    params = {
        "keywords": query,
        "locationName": "London",
        "distancefromLocation": 10,
        "resultsToTake": 100,
    }
    try:
        resp = requests.get(
            "https://www.reed.co.uk/api/1.0/search",
            params=params,
            auth=(REED_API_KEY, ""),
            timeout=15,
        )
        resp.raise_for_status()
        for job in resp.json().get("results", []):
            title = job.get("jobTitle", "")
            if not _title_is_relevant(title):
                continue
            # Filter by date — Reed returns date as "dd/mm/yyyy hh:mm:ss"
            date_str = job.get("date", "")
            try:
                posted = datetime.strptime(date_str[:10], "%d/%m/%Y").replace(tzinfo=timezone.utc)
                if posted < cutoff:
                    continue
            except Exception:
                pass  # if date unparseable, include the job
            jobs.append(Job(
                title=title,
                company=job.get("employerName", "Unknown"),
                location=job.get("locationName", "London"),
                url=f"https://www.reed.co.uk/jobs/{job.get('jobId', '')}",
                source="Reed",
                search_type=search_type,
            ))
    except Exception as e:
        print(f"[Reed/{search_type}] Error: {e}")
    print(f"[Reed/{search_type}] {len(jobs)} jobs")
    return jobs


def search_linkedin(query: str, search_type: str) -> list:
    jobs = []
    encoded = urllib.parse.quote(query)
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={encoded}&location=London%2C%20United%20Kingdom"
        f"&f_TPR=r{DAYS_LOOKBACK * 86400}&start=0"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.find_all("li"):
            try:
                title_el = card.find("h3", class_="base-search-card__title")
                company_el = card.find("h4", class_="base-search-card__subtitle")
                location_el = card.find("span", class_="job-search-card__location")
                link_el = card.find("a", class_="base-card__full-link")
                if not (title_el and company_el and link_el):
                    continue
                location = location_el.text.strip() if location_el else "London, UK"
                clean_url = link_el["href"].split("?")[0]
                jobs.append(Job(
                    title_el.text.strip(),
                    company_el.text.strip(),
                    location,
                    clean_url,
                    "LinkedIn",
                    search_type,
                ))
            except Exception:
                continue
    except Exception as e:
        print(f"[LinkedIn/{search_type}] Error: {e}")
    print(f"[LinkedIn/{search_type}] {len(jobs)} jobs")
    return jobs


def search_greenhouse(keywords: list, search_type: str) -> list:
    jobs = []
    kw_lower = [k.lower() for k in keywords]
    for company in GREENHOUSE_COMPANIES:
        try:
            url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            for job in resp.json().get("jobs", []):
                title = job.get("title", "")
                location = job.get("location", {}).get("name", "")
                if "london" not in location.lower():
                    continue
                if not any(kw in title.lower() for kw in kw_lower):
                    continue
                jobs.append(Job(
                    title,
                    company.title(),
                    location,
                    job.get("absolute_url", ""),
                    "Greenhouse",
                    search_type,
                ))
            time.sleep(0.3)
        except Exception:
            continue
    print(f"[Greenhouse/{search_type}] {len(jobs)} jobs")
    return jobs


def search_wellfound(query: str, search_type: str) -> list:
    """Best-effort scrape — may fail if Wellfound adds JS rendering."""
    jobs = []
    encoded = urllib.parse.quote(query)
    url = f"https://wellfound.com/jobs?q={encoded}&l=London"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for card in soup.find_all("div", {"data-test": "StartupResult"}):
            try:
                title_el = card.find("a", {"data-test": "job-title"})
                company_el = card.find("a", {"data-test": "company-name"})
                if not title_el:
                    continue
                company = company_el.text.strip() if company_el else "Unknown"
                href = title_el.get("href", "")
                full_url = f"https://wellfound.com{href}" if href.startswith("/") else href
                jobs.append(Job(
                    title_el.text.strip(),
                    company,
                    "London, UK",
                    full_url,
                    "Wellfound",
                    search_type,
                ))
            except Exception:
                continue
    except Exception as e:
        print(f"[Wellfound/{search_type}] Error: {e}")
    print(f"[Wellfound/{search_type}] {len(jobs)} jobs")
    return jobs


def search_tavily(query: str, search_type: str) -> list:
    if not TAVILY_API_KEY:
        return []
    jobs = []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": f"{query} London job",
                "search_depth": "basic",
                "max_results": 10,
                "days": DAYS_LOOKBACK,
                "include_domains": TAVILY_DOMAINS,
            },
            timeout=20,
        )
        if not resp.ok:
            print(f"[Tavily/{search_type}] Error {resp.status_code}: {resp.text[:300]}")
            return jobs
        for item in resp.json().get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            if not url or not _title_is_relevant(title):
                continue
            domain = url.split("/")[2].replace("www.", "") if "/" in url else "Unknown"
            jobs.append(Job(title, domain, "London, UK", url, "Tavily", search_type))
    except Exception as e:
        print(f"[Tavily/{search_type}] Error: {e}")
    print(f"[Tavily/{search_type}] {len(jobs)} jobs")
    return jobs


def collect_jobs() -> list:
    exact = []
    exact += search_linkedin("Corporate Development Associate", "Exact")

    wide = []
    wide += search_linkedin(
        "Investment Banking Structured Finance Capital Markets", "Wide"
    )

    return exact + wide


def main() -> None:
    seen = load_seen_jobs()
    all_jobs = collect_jobs()

    new_jobs = []
    seen_this_run: set = set()
    for job in all_jobs:
        if not job.url:
            continue
        if job.job_id in seen or job.job_id in seen_this_run:
            continue
        seen_this_run.add(job.job_id)
        new_jobs.append(job)

    # Priority companies first, then the rest
    new_jobs.sort(key=lambda j: (0 if j.is_priority else 1, j.title))

    priority_count = sum(1 for j in new_jobs if j.is_priority)
    print(f"New jobs to send: {len(new_jobs)} ({priority_count} priority)")

    if not new_jobs:
        print("No new jobs today.")
        return

    summary = f"Job search: {len(new_jobs)} new listing(s)"
    if priority_count:
        summary += f", {priority_count} from priority banks"
    send_telegram(_esc(summary))
    time.sleep(0.5)

    for job in new_jobs:
        try:
            send_telegram(job.telegram_text())
        except Exception as e:
            print(f"Send failed for '{job.title}': {e}")
        finally:
            seen.add(job.job_id)
        time.sleep(2)

    save_seen_jobs(seen)
    print("Done.")


if __name__ == "__main__":
    main()
