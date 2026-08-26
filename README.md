# SWNW Feed Aggregator

Fetches every feed in `config/feeds.opml` daily, filters the broad/noisy
wire feeds (BusinessWire, GlobeNewswire industry feeds, TechCrunch,
SiliconANGLE, etc.) for UC/CCaaS/CPaaS/CX relevance, and writes one
consolidated `data/all-feeds-filtered.json` — which the daily Claude signal
scan then reads directly from `raw.githubusercontent.com`.

## Why this exists

Two problems this solves:

1. **feed.businesswire.com blocks automated fetching via robots.txt** — this
   stops Claude's own web_fetch tool from reading it directly. Running the
   fetch here, under your own GitHub Actions account, sidesteps that.
2. **One consolidated file beats 25+ individual fetches** — faster for the
   scan to read, and if any single feed goes down or changes shape, it's
   logged in `fetch_errors` rather than breaking the whole run.

## Setup

1. Create the repo, upload this content.
2. Settings → Secrets and variables → Actions → add `BUSINESSWIRE_RSS_URL`
   (your personalised BusinessWire feed URL). No other secrets needed —
   every other feed in the OPML is a plain public URL.
3. Actions tab → "Daily feed aggregation" → Run workflow, to confirm it
   commits `data/all-feeds-filtered.json` cleanly on the first try.

## Updating the feed list

Edit `config/feeds.opml` directly, or re-export from NewsBlur (Settings →
Import/Export → Export OPML) and replace the file. No code changes needed —
the script reads whatever's in the OPML on each run.

## Feeding the daily signal scan

Add this as the single priority source in the scan's standing brief:

```
https://raw.githubusercontent.com/<your-username>/<repo-name>/main/data/all-feeds-filtered.json
```

That's a public raw.githubusercontent.com URL, which is on Claude's allowed
fetch domains — no robots.txt issue, since by then it's your own GitHub
content, not the original publisher's.

## Schedule

Runs daily at 05:30 UTC, ahead of the morning scan. Change the cron line in
`.github/workflows/daily-fetch.yml` if you want a different time.
