"""
Fetch every feed listed in config/feeds.opml, filter for recency (and, for
the broad/noisy wire feeds specifically, for UC/CCaaS/CPaaS/CX relevance),
and write one consolidated JSON that the daily signal scan reads from
raw.githubusercontent.com.

Why this exists as a GitHub Actions job rather than a direct Claude fetch:
- feed.businesswire.com disallows automated fetching via robots.txt, which
  blocks Claude's own web_fetch tool. This runs under the user's own
  GitHub Actions account instead.
- Consolidating ALL feeds into one JSON (rather than the scan fetching 25+
  URLs individually every morning) is faster and more robust: one file,
  one fetch, and any single feed going down/changing shape doesn't break
  the whole scan (see FETCH ERRORS handling below).

Secrets required (Settings > Secrets and variables > Actions):
  BUSINESSWIRE_RSS_URL   - the personalised BusinessWire feed URL (kept as
                            a secret since it's tied to the account; every
                            other feed in the OPML is a plain public URL
                            and doesn't need one)

To add/remove/change feeds: edit config/feeds.opml directly (same file
format NewsBlur exports), or re-export from NewsBlur and replace it.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import feedparser
import requests

RECENCY_WINDOW_HOURS = 48  # keep items published within this window
FALLBACK_ITEM_COUNT = 10   # if a feed has no usable dates, take the N newest entries
REQUEST_TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SWNW-signal-scan/1.0)"}

# Feeds that are broad, multi-industry wires where most items are NOT
# relevant to UC/CCaaS/CPaaS/CX - these get the keyword filter applied.
# Everything else in the OPML is already narrowly scoped by its own editorial
# focus (CX Today, UC Today, vendor blogs, the Google News keyword searches,
# the CPaaS Alliance feed, etc.) and is passed through unfiltered.
BROAD_FEEDS_NEEDING_FILTER = {
    "My Business Wire News",
    "GlobeNewswire - Technology Industry News",
    "GlobeNewswire - Telecommunications Industry News",
    "TechCrunch",
    "SiliconANGLE",
    "Verdict",
    "Technology News For IT Channel Partners and Solution Providers",
}

KEYWORDS = [
    # CCaaS / contact centre
    "contact center", "contact centre", "ccaas", "call center", "call centre",
    "customer experience", "cx platform", "genesys", "nice ", "five9",
    "talkdesk", "sprinklr", "content guru", "amazon connect",
    # UC / collaboration
    "unified communications", "ucaas", "video conferencing", "collaboration platform",
    "ringcentral", "8x8", "dialpad", "goto connect", "nextiva", "mitel",
    "zoom ", "webex", "microsoft teams", "neat ", "video technologies",
    # CPaaS / messaging/voice API
    "cpaas", "communications platform as a service", "twilio", "infobip",
    "sinch", "vonage", "bandwidth.com", "telnyx", "plivo", " bird ",
    # CX suite / adjacent
    "customer service platform", "customer engagement platform",
    "conversational ai", "voice ai", "agentic ai customer",
]


def is_relevant(title: str, summary: str) -> bool:
    haystack = f"{title} {summary}".lower()
    return any(kw in haystack for kw in KEYWORDS)


def load_feed_list(opml_path: str) -> list[dict]:
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds = []
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl")
        title = outline.get("text") or outline.get("title") or xml_url
        if xml_url:
            feeds.append({"name": title, "url": xml_url})
    return feeds


def entry_datetime(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_feed(name: str, url: str, rss_url_overrides: dict) -> dict:
    # BusinessWire's personalised URL is injected from a secret rather than
    # whatever's literally in the OPML, since NewsBlur's copy may be stale
    # or the account-specific query string may rotate.
    actual_url = rss_url_overrides.get(name, url)

    try:
        resp = requests.get(actual_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, one feed shouldn't kill the run
        return {"name": name, "url": actual_url, "error": str(exc), "items": []}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENCY_WINDOW_HOURS)
    needs_filter = name in BROAD_FEEDS_NEEDING_FILTER

    dated_entries = []
    undated_entries = []
    for entry in parsed.entries:
        dt = entry_datetime(entry)
        if dt is not None:
            dated_entries.append((dt, entry))
        else:
            undated_entries.append(entry)

    if dated_entries:
        recent = [e for dt, e in dated_entries if dt >= cutoff]
        recent.sort(key=lambda e: entry_datetime(e) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    else:
        # No usable dates anywhere in this feed - fall back to "newest N as listed"
        recent = undated_entries[:FALLBACK_ITEM_COUNT]

    items = []
    for entry in recent:
        title = entry.get("title", "")
        summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()[:500]

        if needs_filter and not is_relevant(title, summary):
            continue

        items.append(
            {
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": summary,
            }
        )

    return {"name": name, "url": actual_url, "error": None, "items": items}


def main() -> int:
    opml_path = os.path.join(os.path.dirname(__file__), "..", "config", "feeds.opml")
    feeds = load_feed_list(opml_path)

    rss_url_overrides = {}
    bw_url = os.environ.get("BUSINESSWIRE_RSS_URL")
    if bw_url:
        rss_url_overrides["My Business Wire News"] = bw_url

    results = []
    for feed in feeds:
        result = fetch_feed(feed["name"], feed["url"], rss_url_overrides)
        results.append(result)
        status = f"ERROR: {result['error']}" if result["error"] else f"{len(result['items'])} items"
        print(f"{feed['name']}: {status}")

    errors = [r for r in results if r["error"]]
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recency_window_hours": RECENCY_WINDOW_HOURS,
        "feeds_fetched": len(feeds),
        "feeds_with_errors": len(errors),
        "fetch_errors": [{"name": e["name"], "url": e["url"], "error": e["error"]} for e in errors],
        "feeds": [{"name": r["name"], "items": r["items"]} for r in results],
    }

    os.makedirs("data", exist_ok=True)
    with open("data/all-feeds-filtered.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    total_items = sum(len(r["items"]) for r in results)
    print(f"\nDone: {total_items} items across {len(feeds)} feeds ({len(errors)} feeds errored).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
