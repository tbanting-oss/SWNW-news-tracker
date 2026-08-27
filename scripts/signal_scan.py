#!/usr/bin/env python3
"""
SWNW signal scan — runs inside GitHub Actions, calls the Claude API to turn the
aggregated feed into rolling-log rows, then writes docs/log-data.json and
docs/index.html. Everything lives in the repo: Actions reads and writes it
natively, GitHub Pages serves the log at a stable URL. No claude.ai artifact,
no Cowork network blocks.

Inputs  : data/all-feeds-filtered.json   (built by daily-fetch.yml)
State   : docs/log-data.json             (rolling 7-day source of truth)
Outputs : docs/log-data.json (updated), docs/index.html (rendered)

Env:
  ANTHROPIC_API_KEY   required
  ANTHROPIC_MODEL     optional, defaults below
"""

import json
import os
import re
import sys
import time
import html
from datetime import datetime, timedelta, timezone

import requests

FEED_PATH = "data/all-feeds-filtered.json"
LOG_DATA_PATH = "docs/log-data.json"
LOG_HTML_PATH = "docs/index.html"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
API_URL = "https://api.anthropic.com/v1/messages"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

RETENTION_DAYS = 7
MAX_ROWS_PER_RUN = 8          # hard cap so one run can't flood the log
API_TIMEOUT = 120
API_RETRIES = 3

# Pill taxonomy — must match the CSS classes in render_html()
VALID_PILLS = {"Signal", "Signal+", "Pattern", "Teardown"}
VALID_CATS = {"CCaaS", "UC", "CPaaS", "CX", "AI"}

SYSTEM_PROMPT = """\
You are a strategic analyst covering UC / CCaaS / CPaaS / CX / enterprise-AI \
markets for a UK CIO/ITDM audience. Voice: analytical, sceptical, evidence-led, \
Grade 10-12, zero hype, zero fluff. You default DOWN on significance, never up.

You are handed feed items that are NOT already in the rolling log. Select ONLY \
genuine signal — a discrete vendor move, launch, deal, filing, financial result, \
personnel change, or a real pattern. Reject vendor trend-marketing, listicles, \
evergreen comparisons, and recycled thought-leadership.

For each item you keep, return an object with:
  pill    : one of "Signal", "Signal+", "Pattern", "Teardown"
            - Signal   = a real but ordinary discrete move (neutral)
            - Signal+  = a materially important move worth stopping for
            - Pattern  = multiple items sharing one thread / a market shift
            - Teardown = something that deserves a sceptical takedown
            Default to "Signal". Vendor-authored trend research with no discrete
            move is ALWAYS "Signal" and must set "vendor_flag": true.
  cat     : one of "CCaaS", "UC", "CPaaS", "CX", "AI"
  headline: 5-9 words, verb-led, insight-first. Not the source's headline verbatim.
  sowhat  : ONE sentence, the strategic "so what" for a buyer. Concrete, no hype.
  url     : the item's source URL, unchanged
  vendor_flag : true only for vendor-authored trend research with no discrete move

Return STRICT JSON: {"rows": [ ... ], "pattern_watch": "<one line or empty>"}
"pattern_watch" is one line naming a thread if two or more kept items share one; \
otherwise "". Keep at most %d rows — the strongest signal only. If nothing clears \
the bar, return {"rows": [], "pattern_watch": ""}.
""" % MAX_ROWS_PER_RUN


def now_utc():
    return datetime.now(timezone.utc)


def run_label(dt):
    return "AM" if dt.hour < 12 else "PM"


def norm_url(u):
    if not u:
        return ""
    u = u.strip().lower()
    u = re.sub(r"[?#].*$", "", u)
    u = re.sub(r"/+$", "", u)
    return u


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def collect_feed_items(feed):
    items = []
    for f in feed.get("feeds", []):
        source = f.get("name", "")
        for it in f.get("items", []):
            items.append({
                "source": source,
                "title": it.get("title", ""),
                "url": it.get("link", ""),
                "published": it.get("published", ""),
                "summary": (it.get("summary", "") or "")[:400],
            })
    return items


def seen_urls(log_data):
    seen = set()
    for day in log_data.get("days", []):
        for row in day.get("rows", []):
            seen.add(norm_url(row.get("url", "")))
    return seen


def call_claude(items):
    """Return (rows, pattern_watch). Rows validated & de-duped by caller."""
    if not items:
        return [], ""

    payload_items = [
        {"source": i["source"], "title": i["title"], "url": i["url"],
         "published": i["published"], "summary": i["summary"]}
        for i in items
    ]
    user_msg = (
        "Feed items not yet in the log (JSON):\n\n"
        + json.dumps(payload_items, ensure_ascii=False)
        + "\n\nReturn strict JSON as instructed."
    )

    body = {
        "model": MODEL,
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=API_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError(f"API {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            parsed = extract_json(text)
            return parsed.get("rows", []), parsed.get("pattern_watch", "")
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            if attempt < API_RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Claude API failed after {API_RETRIES} attempts: {last_err}")


def extract_json(text):
    text = text.strip()
    # tolerate ```json fences or leading prose
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"rows": [], "pattern_watch": ""}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"rows": [], "pattern_watch": ""}


def validate_rows(rows, already_seen, label):
    clean = []
    batch_seen = set()
    for r in rows:
        pill = r.get("pill", "Signal")
        cat = r.get("cat", "")
        url = r.get("url", "")
        nurl = norm_url(url)
        if pill not in VALID_PILLS:
            pill = "Signal"
        if cat not in VALID_CATS:
            continue
        if not nurl or nurl in already_seen or nurl in batch_seen:
            continue
        headline = (r.get("headline") or "").strip()
        sowhat = (r.get("sowhat") or "").strip()
        if not headline or not sowhat:
            continue
        batch_seen.add(nurl)
        clean.append({
            "pill": pill,
            "cat": cat,
            "headline": headline,
            "sowhat": sowhat,
            "url": url,
            "vendor_flag": bool(r.get("vendor_flag", False)),
            "run": label,
        })
        if len(clean) >= MAX_ROWS_PER_RUN:
            break
    return clean


def upsert_today(log_data, new_rows, pattern_watch, dt):
    today_iso = dt.strftime("%Y-%m-%d")
    today_label = dt.strftime("%a %d %b %Y")
    days = log_data.setdefault("days", [])

    today = next((d for d in days if d.get("iso") == today_iso), None)
    if today is None:
        today = {"iso": today_iso, "label": today_label, "rows": [], "pattern_watch": ""}
        days.insert(0, today)

    today["rows"].extend(new_rows)
    if pattern_watch:
        today["pattern_watch"] = pattern_watch

    # prune to retention window
    cutoff = (dt - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    log_data["days"] = [d for d in days if d.get("iso", "") >= cutoff]
    log_data["days"].sort(key=lambda d: d.get("iso", ""), reverse=True)
    log_data["updated"] = dt.isoformat()
    return log_data


PILL_CLASS = {
    "Signal": "pill-signal",
    "Signal+": "pill-signalplus",
    "Pattern": "pill-pattern",
    "Teardown": "pill-teardown",
}


def render_html(log_data):
    days = log_data.get("days", [])
    updated = log_data.get("updated", "")
    try:
        updated_disp = datetime.fromisoformat(updated).strftime("%a %d %b %Y, %H:%M UTC") if updated else ""
    except ValueError:
        updated_disp = updated
    covers = ""
    if days:
        newest = days[0].get("label", "")
        oldest = days[-1].get("label", "")
        covers = newest if newest == oldest else f"{oldest} — {newest}"

    today_iso = days[0]["iso"] if days else ""

    sections = []
    for d in days:
        is_today = " is-today" if d.get("iso") == today_iso else ""
        rows_html = []
        for row in d.get("rows", []):
            pill_cls = PILL_CLASS.get(row["pill"], "pill-signal")
            flag = ' <span class="vendorflag">vendor research</span>' if row.get("vendor_flag") else ""
            run = html.escape(row.get("run", ""))
            rows_html.append(f"""
        <div class="row">
          <div class="pillcol"><span class="pill {pill_cls}">{html.escape(row['pill'])}</span></div>
          <div class="body">
            <a class="headline" href="{html.escape(row['url'])}" target="_blank" rel="noopener">{html.escape(row['headline'])}</a>{flag}
            <div class="sowhat">{html.escape(row['sowhat'])}</div>
          </div>
          <div class="meta"><span class="cat">{html.escape(row['cat'])}</span><span class="run">{run}</span></div>
        </div>""")
        pw = d.get("pattern_watch", "")
        pw_html = f'\n        <div class="pattern-watch"><b>Pattern watch:</b> {html.escape(pw)}</div>' if pw else ""
        if not d.get("rows"):
            rows_html.append('\n        <div class="empty">No material signal.</div>')
        sections.append(f"""
      <section class="day{is_today}">
        <h2 class="day-head{is_today}">{html.escape(d.get('label',''))}</h2>{pw_html}
        {''.join(rows_html)}
      </section>""")

    return TEMPLATE.format(
        updated=html.escape(updated_disp),
        covers=html.escape(covers),
        sections="".join(sections) if sections else '<p class="empty">No entries yet.</p>',
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SWNW Signal Log — UC / CCaaS / CPaaS / CX</title>
<style>
  :root {{
    --bg:#0f1115; --card:#171a21; --line:#262b36; --ink:#e8eaed; --mut:#9aa3b2;
    --signal:#3b82f6; --signalplus:#0ea5a4; --pattern:#8b5cf6; --teardown:#ef4444; --flag:#b45309;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#f6f7f9; --card:#ffffff; --line:#e3e6eb; --ink:#14171c; --mut:#5b6472; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:28px 20px 64px; }}
  header.mast {{ border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:8px; }}
  .mast h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-.2px; }}
  .mast .sub {{ color:var(--mut); font-size:13px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 4px; }}
  .pill {{ display:inline-block; font-size:12px; font-weight:700; padding:3px 10px;
    border-radius:999px; color:#fff; letter-spacing:.2px; white-space:nowrap; }}
  .pill-signal {{ background:var(--signal); }}
  .pill-signalplus {{ background:var(--signalplus); }}
  .pill-pattern {{ background:var(--pattern); }}
  .pill-teardown {{ background:var(--teardown); }}
  .legend .lg {{ color:var(--mut); font-size:12px; align-self:center; }}
  section.day {{ margin-top:26px; }}
  .day-head {{ font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:var(--mut);
    margin:0 0 10px; font-weight:700; }}
  .day-head.is-today {{ color:var(--ink); }}
  .day-head.is-today::after {{ content:" · today"; color:var(--signal); }}
  .pattern-watch {{ font-size:13px; color:var(--mut); background:var(--card);
    border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:0 0 12px; }}
  .row {{ display:grid; grid-template-columns:96px 1fr auto; gap:12px; align-items:start;
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:12px 14px; margin-bottom:10px; }}
  .headline {{ color:var(--ink); text-decoration:none; font-weight:650; font-size:15px; }}
  .headline:hover {{ text-decoration:underline; }}
  .sowhat {{ color:var(--mut); font-size:13.5px; margin-top:3px; }}
  .meta {{ text-align:right; display:flex; flex-direction:column; gap:4px; align-items:flex-end; }}
  .cat {{ font-size:11px; font-weight:700; color:var(--mut); border:1px solid var(--line);
    border-radius:6px; padding:2px 7px; }}
  .run {{ font-size:10px; color:var(--mut); }}
  .vendorflag {{ font-size:10px; font-weight:700; color:#fff; background:var(--flag);
    padding:1px 6px; border-radius:5px; vertical-align:middle; }}
  .empty {{ color:var(--mut); font-size:13px; font-style:italic; }}
  @media (max-width:560px) {{
    .row {{ grid-template-columns:70px 1fr; }}
    .meta {{ grid-column:2; flex-direction:row; gap:8px; align-items:center; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="mast">
      <h1>SWNW Signal Log</h1>
      <div class="sub">UC · CCaaS · CPaaS · CX · AI — rolling 7 days · Updated {updated} · Covers {covers}</div>
    </header>
    <div class="legend">
      <span class="lg">Legend:</span>
      <span class="pill pill-signal">Signal</span>
      <span class="pill pill-signalplus">Signal+</span>
      <span class="pill pill-pattern">Pattern</span>
      <span class="pill pill-teardown">Teardown</span>
      <span class="lg">— default-down, never inflated. Signal+ = stop for it. Pattern = shared thread. Teardown = sceptical takedown.</span>
    </div>
    <!-- LOG-START -->{sections}
  </div>
</body>
</html>
"""


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    feed = load_json(FEED_PATH, {})
    if not feed:
        print(f"ERROR: {FEED_PATH} missing or empty", file=sys.stderr)
        return 1

    log_data = load_json(LOG_DATA_PATH, {"days": [], "updated": ""})
    dt = now_utc()
    label = run_label(dt)

    already = seen_urls(log_data)
    all_items = collect_feed_items(feed)
    fresh = [i for i in all_items if norm_url(i["url"]) and norm_url(i["url"]) not in already]
    print(f"{len(all_items)} feed items, {len(fresh)} not already in log")

    if fresh:
        raw_rows, pattern_watch = call_claude(fresh)
        new_rows = validate_rows(raw_rows, already, label)
    else:
        new_rows, pattern_watch = [], ""

    print(f"{len(new_rows)} new rows kept ({label} run)")

    log_data = upsert_today(log_data, new_rows, pattern_watch, dt)

    os.makedirs("docs", exist_ok=True)
    with open(LOG_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
    with open(LOG_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(log_data))

    print(f"Wrote {LOG_DATA_PATH} and {LOG_HTML_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
