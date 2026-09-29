"""
Memory of which feed items the signal scan has already sent to the model.

The signal scan only remembers rows the model KEPT, so an item it rejected is sent again on every run
while it stays in the feed window. This module keeps a small file of what has been sent, and how many
times, so the scan can cap repeats. It has no network or model code and is tested on its own.

An item is identified by its address plus its title, not the address alone: some feeds (the Microsoft 365
Roadmap) put many different items on one address, and a new item there must not be mistaken for an old one.

The key function is duplicated in buyer-intent-automation/scripts/token_audit.py so the history replay
measures the same thing; that script checks the two agree when it runs.
"""
import re
import json
import hashlib
import datetime as dt

SENT_PATH = "data/sent-items.json"
KEEP_DAYS = 10          # far longer than the 48 hour feed window, so nothing recent is forgotten early


def norm_url(u):
    u = str(u or "").strip().lower()
    u = re.sub(r"[?#].*$", "", u)
    return re.sub(r"/+$", "", u)


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def item_key(item):
    raw = norm_url(item.get("url", "")) + "|" + norm_title(item.get("title", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load(path=SENT_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = {}
    if not isinstance(reg.get("items"), dict):
        reg["items"] = {}
    return reg


def send_count(reg, item):
    return int(reg["items"].get(item_key(item), {}).get("n", 0))


def select(items, reg, max_sends):
    """-> (to_send, held_back). max_sends <= 0 means no cap, which is the scan's old behaviour."""
    if max_sends <= 0:
        return list(items), []
    send, held = [], []
    for i in items:
        (send if send_count(reg, i) < max_sends else held).append(i)
    return send, held


def record(reg, items, today):
    """Count one more send for each item. Call only after the model call succeeded."""
    for i in items:
        e = reg["items"].setdefault(item_key(i), {"n": 0, "first": today.isoformat()})
        e["n"] = int(e.get("n", 0)) + 1
    return reg


def prune(reg, today, keep_days=KEEP_DAYS):
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    reg["items"] = {k: v for k, v in reg["items"].items() if str(v.get("first", "")) >= cutoff}
    return reg


def save(reg, path=SENT_PATH, updated=""):
    reg["updated"] = updated
    with open(path, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=0, sort_keys=True)
