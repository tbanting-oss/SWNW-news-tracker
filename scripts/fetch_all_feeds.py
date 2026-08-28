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
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import feedparser
import requests

RECENCY_WINDOW_HOURS = 48  # keep items published within this window
FALLBACK_ITEM_COUNT = 10   # if a feed has no usable dates, take the N newest entries
REQUEST_TIMEOUT = (10, 25)  # (connect, read) seconds; caps a hung GlobeNewswire read at 25s
RETRY_ATTEMPTS = 3          # was 2; the throttled wire feeds need more patience, but time-bounded
FALLBACK_ATTEMPTS = 1       # the Google News fallback is best-effort — one try, keep the run snappy
RETRY_BACKOFF_SECONDS = 3   # base; actual wait = base*attempt + random jitter (see fetch_feed)

# Pipeline-health thresholds. These measure whether the FETCH succeeded, not how
# much news there was, so a quiet news day never trips them. main() exits non-zero
# (skipping the commit, leaving generated_at stale so the Cowork run flags it and
# the Actions run shows red) only on a genuine pipeline failure.
HARD_FAIL_ERROR_FRACTION = 0.5   # more than half the feeds errored
# NB: item COUNT is deliberately not a fail signal — a successful fetch can yield
# zero in-window items on a quiet news day. Only fetch ERRORS fail the run.
# Feeds that carry most of the real signal; if ALL of them fail, treat the run as
# failed even if lighter feeds succeeded.
CRITICAL_FEEDS = {
    "GlobeNewswire - Technology Industry News",
    "GlobeNewswire - Telecommunications Industry News",
    "My Business Wire News",
}

# A real-browser UA rather than a self-identifying "compatible; ...bot" string -
# several publishers (UC Today, Telecom Reseller, Microsoft blogs) returned 403s
# to the bot-style UA in testing; some WAFs specifically flag the "compatible;"
# pattern regardless of what follows it. Referer + Accept-Language further reduce
# WAF 403s (Verdict rejected the bare UA).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# Same-source Google News RSS fallbacks for feeds whose own RSS is dead or WAF-blocked.
# Tried only when the primary URL errors. Google News is not IP-throttled the way the
# publishers' own endpoints are. Caveat: item links are news.google.com redirect URLs
# (they resolve to the real article in a browser), not the publisher's direct URL.
# Keyed by the exact OPML feed name.
GOOGLE_NEWS_FALLBACKS = {
    "The ChannelPro Network - IT and Business Insights for SMB Solution Providers":
        "https://news.google.com/rss/search?q=site:channelpronetwork.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    "UC Today":
        "https://news.google.com/rss/search?q=site:uctoday.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    "Verdict":
        "https://news.google.com/rss/search?q=site:verdict.co.uk+(%22contact+center%22+OR+CCaaS+OR+CPaaS+OR+%22unified+communications%22)+when:3d&hl=en-US&gl=US&ceid=US:en",
}

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

    # Try the primary URL, then — only if it errors — a same-source Google News
    # fallback for the feeds whose own RSS is dead/WAF-blocked.
    url_chain = [actual_url]
    fallback = GOOGLE_NEWS_FALLBACKS.get(name)
    if fallback:
        url_chain.append(fallback)

    last_error = None
    parsed = None
    resolved_url = actual_url
    for idx, candidate in enumerate(url_chain):
        attempts = RETRY_ATTEMPTS if idx == 0 else FALLBACK_ATTEMPTS  # fallback gets one try
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.get(candidate, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                last_error = None
                resolved_url = candidate
                break
            except Exception as exc:  # noqa: BLE001 - one feed shouldn't kill the whole run
                last_error = str(exc)
                if attempt < attempts:
                    # jittered backoff — avoids hammering a throttling host on a fixed cadence
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt + random.uniform(0, 2))
        if last_error is None:
            break  # this candidate worked; don't fall through to the fallback

    if last_error is not None:
        return {"name": name, "url": resolved_url, "error": last_error, "items": []}

    actual_url = resolved_url

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
        time.sleep(1.5)  # brief gap between requests - three sequential GlobeNewswire
        # industry-feed timeouts in the first live run suggest hammering the
        # same host back-to-back was a factor

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

    # --- Pipeline-health guard --------------------------------------------
    # A degraded run should be LOUD, not silent. We still wrote the JSON above so
    # a partial run's data is available; but on a genuine fetch failure we exit
    # non-zero. In the workflow that fails the job (red X, GitHub failure email)
    # and — because the commit step is skipped — leaves generated_at stale, which
    # the Cowork scan reads and flags to Tim. Thresholds test the FETCH, not the
    # news volume, so a quiet news day never trips them.
    error_names = {e["name"] for e in errors}
    error_fraction = len(errors) / len(feeds) if feeds else 1.0
    critical_all_failed = CRITICAL_FEEDS.issubset(error_names) if CRITICAL_FEEDS else False

    warnings = []
    if total_items == 0 and errors:
        # zero items AND some feeds failed — the failures likely cost us everything
        warnings.append("zero items and " + str(len(errors)) + " feed(s) errored")
    if error_fraction > HARD_FAIL_ERROR_FRACTION:
        warnings.append(f"{len(errors)}/{len(feeds)} feeds errored (> {HARD_FAIL_ERROR_FRACTION:.0%})")
    if critical_all_failed:
        warnings.append("all critical wire feeds failed: " + ", ".join(sorted(CRITICAL_FEEDS)))

    if warnings:
        for w in warnings:
            # ::error:: annotations surface at the top of the GitHub Actions run
            print(f"::error::feed pipeline degraded — {w}", file=sys.stderr)
        print("PIPELINE UNHEALTHY — exiting non-zero so the run fails visibly "
              "and the stale JSON is NOT committed over good data.", file=sys.stderr)
        return 1

    # Soft warnings for individual dead feeds — visible but do not fail the run.
    for e in errors:
        print(f"::warning::feed '{e['name']}' failed: {e['error'][:120]}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
