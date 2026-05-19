#!/usr/bin/env python3
"""Daily job search: Corporate Development / Finance roles in London."""

import hashlib
import json
import os
import time
import urllib.parse
from dataclasses import dataclass

import feedparser
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
REED_API_KEY = os.environ.get("REED_API_KEY", "")
SEEN_JOBS_FILE = "job_search/seen_jobs.json"
MAX_SEEN_JOBS = 2000

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

    def telegram_text(self) -> str:
        label = f"{self.source} · {self.search_type}"
        title = _esc(self.title)
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


def search_reed(query: str, search_type: str) -> list:
    if not REED_API_KEY:
        return []
    jobs = []
    params = {
        "keywords": query,
        "locationName": "London",
        "distancefromLocation": 10,
        "postedByRecruitmentAgency": False,
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
            jobs.append(Job(
                title=job.get("jobTitle", ""),
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
        "&f_TPR=r691200&start=0"
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


def search_google(query: str, search_type: str) -> list:
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []
    jobs = []
    full_query = f"{query} London jobs"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": full_query,
        "num": 10,
        "dateRestrict": "d8",
    }
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for item in items:
            title = item.get("title", "")
            url = item.get("link", "")
            if not url:
                continue
            # Try to extract company from pagemap or snippet
            pagemap = item.get("pagemap", {})
            metatags = pagemap.get("metatags", [{}])
            site_name = metatags[0].get("og:site_name", "") if metatags else ""
            company = site_name if site_name else url.split("/")[2].replace("www.", "")
            jobs.append(Job(title, company, "London, UK", url, "Google", search_type))
    except Exception as e:
        print(f"[Google/{search_type}] Error: {e}")
    print(f"[Google/{search_type}] {len(jobs)} jobs")
    return jobs


def collect_jobs() -> list:
    exact = []
    exact += search_reed("Corporate Development Associate", "Exact")
    exact += search_linkedin("Corporate Development Associate", "Exact")
    exact += search_greenhouse(EXACT_KEYWORDS, "Exact")
    exact += search_google('"Corporate Development Associate"', "Exact")

    wide = []
    wide += search_reed("Investment Banking Associate", "Wide")
    wide += search_reed("Structured Finance London", "Wide")
    wide += search_reed("Capital Markets Associate", "Wide")
    wide += search_linkedin(
        "Investment Banking Structured Finance Capital Markets", "Wide"
    )
    wide += search_greenhouse(WIDE_KEYWORDS, "Wide")
    wide += search_google('"Investment Banking" OR "Structured Finance" OR "Capital Markets"', "Wide")

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

    print(f"New jobs to send: {len(new_jobs)}")

    if not new_jobs:
        print("No new jobs today.")
        return

    send_telegram(_esc(f"Job search: {len(new_jobs)} new listing(s) found today."))
    time.sleep(0.5)

    for job in new_jobs:
        try:
            send_telegram(job.telegram_text())
        except Exception as e:
            print(f"Send failed for '{job.title}': {e}")
        finally:
            seen.add(job.job_id)
        time.sleep(0.5)

    save_seen_jobs(seen)
    print("Done.")


if __name__ == "__main__":
    main()
