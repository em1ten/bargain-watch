#!/usr/bin/env python3
"""
Bargain Watch
-------------
Scans Vinted for a set of core searches plus a daily rotation of
"discovery" brands, scores every listing, and writes one ranked feed
for the dashboard (docs/data.json). Notifications go out via ntfy.sh
for new finds on instant watches, and queue for the daily digest on
digest watches.

The dashboard is deliberately read-only: all configuration lives in
config.json in the repo (protected by your GitHub login), so the page
itself has no inputs, tokens, or write access to anything.

Scoring (roughly 0-100+, higher = better find):
  + discount vs RRP        (up to 60)
  + at/under flat bargain price (+15)
  + condition               (new with tags +30, new without tags +24, very good +8, good +3)
  + trustworthy seller      (+5)
  + matches your size       (+20)
  + new since last scan     (+5)

Discovery: each day, `discovery_per_day` brands are picked from the
discovery_pool (rotating by day of year, so the mix changes daily
without any input) and scanned alongside the core watches. Their
finds are tagged so the dashboard can show where they came from.
"""

import json
import os
import time
import unicodedata
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"
DATA_PATH = DOCS_DIR / "data.json"
SEEN_PATH = ROOT / "seen_ids.json"
DIGEST_PATH = ROOT / "digest_pending.json"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

CONDITION_SCORES = {
    "New with tags": 30,
    "New without tags": 24,
    "Very good": 8,
    "Good": 3,
}
CONDITION_BADGE = {
    "New with tags": "BNWT",
    "New without tags": "UNWORN",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_json_set(path):
    if path.exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def new_session(domain):
    """Vinted's site sets anti-bot cookies on first visit. Grab those
    before calling the API, same as a real browser would."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    session.get(f"https://{domain}/", timeout=15)
    return session


def pick_discovery(pool, per_day):
    """Rotate through the pool by day of year, so the selection changes
    daily and cycles through everything over time."""
    if not pool or per_day <= 0:
        return []
    start = date.today().timetuple().tm_yday % len(pool)
    picked = []
    for i in range(min(per_day, len(pool))):
        picked.append(pool[(start + i) % len(pool)])
    return picked


def run_search(session, domain, watch, currency, per_page):
    params = {
        "search_text": watch["search_text"],
        "order": "newest_first",
        "per_page": per_page,
        "currency": currency,
    }
    if watch.get("price_to"):
        params["price_to"] = watch["price_to"]
    if watch.get("price_from"):
        params["price_from"] = watch["price_from"]

    url = f"https://{domain}/api/v2/catalog/items"
    resp = session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("items", [])


def passes_filters(item, exclude_terms):
    title = (item.get("title") or "").lower()
    return not any(term.lower() in title for term in exclude_terms)


def normalize_brand(s):
    """Lowercase, strip accents/punctuation, so 'C.P. Company' and 'CP Company'
    (or 'Klättermusen' and 'Klattermusen') compare equal."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c.lower() for c in s if c.isalnum())


def brand_matches(item, watch):
    """Cross-check Vinted's own brand tag on the listing against the brand
    being watched, rather than trusting free-text search alone. This is what
    catches things like 'Iron Heart' text-matching iron-on patches, or
    'Acne Studios' text-matching acne skincare pads - Vinted's search matches
    loosely on words in the title, but the brand_title field is what the
    seller actually tagged the item as."""
    if not watch.get("require_brand_match", True):
        return True
    actual = normalize_brand(item.get("brand_title") or "")
    if not actual:
        return False  # no brand tag at all - usually generic/mistagged junk
    candidates = watch.get("brand_match") or [watch["name"]]
    for candidate in candidates:
        expected = normalize_brand(candidate)
        if expected and (expected in actual or actual in expected):
            return True
    return False


def size_matches(size_title, size_terms):
    if not size_terms:
        return False
    size = (size_title or "").strip().lower()
    return bool(size) and any(s.lower() in size for s in size_terms)


def build_card(item, domain, watch, source, my_sizes, threshold_pct, max_price, is_new):
    photo = (item.get("photo") or {}).get("url", "")
    price_obj = item.get("total_item_price") or item.get("price") or {}
    amount = price_obj.get("amount")
    currency = price_obj.get("currency_code", "")

    user = item.get("user") or {}
    seller = {
        "login": user.get("login", ""),
        "feedback_count": user.get("feedback_count") or user.get("positive_feedback_count") or 0,
        "reputation": user.get("feedback_reputation"),
    }

    price_amount = None
    if amount is not None:
        try:
            price_amount = float(amount)
        except (TypeError, ValueError):
            pass

    rrp = watch.get("rrp")
    discount_pct = None
    if rrp and price_amount is not None and rrp > 0:
        discount_pct = round((1 - price_amount / rrp) * 100)

    condition = (item.get("status") or "").strip()
    size_title = (item.get("size_title") or "").strip()
    size_terms = my_sizes.get(watch.get("size_category", ""), [])
    my_size = size_matches(size_title, size_terms)

    photo_obj = item.get("photo") or {}
    listed_at = (photo_obj.get("high_resolution") or {}).get("timestamp") or item.get("created_at_ts")
    try:
        listed_at = int(listed_at) if listed_at is not None else None
    except (TypeError, ValueError):
        listed_at = None

    score = 0
    if discount_pct is not None and discount_pct > 0:
        score += min(discount_pct, 60)
    if max_price is not None and price_amount is not None and price_amount <= max_price:
        score += 15
    score += CONDITION_SCORES.get(condition, 0)
    rep = seller.get("reputation")
    if isinstance(rep, (int, float)) and rep >= 0.95 and seller["feedback_count"] >= 10:
        score += 5
    if my_size:
        score += 20
    if is_new:
        score += 5

    is_bargain = (discount_pct is not None and discount_pct >= threshold_pct) or (
        max_price is not None and price_amount is not None and price_amount <= max_price
    )
    if discount_pct is not None and discount_pct >= threshold_pct:
        bargain_reason = f"-{discount_pct}%"
    elif is_bargain:
        bargain_reason = f"Under {currency} {int(max_price)}"
    else:
        bargain_reason = None

    return {
        "id": item.get("id"),
        "title": item.get("title", "").strip(),
        "watch": watch["name"],
        "brand": (item.get("brand_title") or "").strip(),
        "size": size_title,
        "condition": condition,
        "price": f"{amount} {currency}" if amount else "",
        "photo": photo,
        "url": f"https://{domain}/items/{item.get('id')}",
        "seller": seller,
        "discount_pct": discount_pct,
        "score": score,
        "my_size": my_size,
        "is_new": is_new,
        "is_bargain": is_bargain,
        "bargain_reason": bargain_reason,
        "source": source,
        "listed_at": listed_at,
        "condition_badge": CONDITION_BADGE.get(condition),
    }


def notify_ntfy(topic, title, cards):
    if not topic or not cards:
        return
    lines = [f"{c['watch']} — {c['price']}: {c['title']}" for c in cards[:5]]
    message = "\n".join(lines)
    if len(cards) > 5:
        message += f"\n…and {len(cards) - 5} more"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "default",
                "Tags": "shirt",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  ! ntfy push failed: {e}")


def main():
    config = load_config()
    domain = config.get("domain", "www.vinted.co.uk")
    currency = config.get("currency", "GBP")
    per_page = config.get("max_items_per_watch", 40)
    threshold_pct = config.get("bargain_threshold_pct", 50)
    max_price = config.get("bargain_max_price")
    my_sizes = config.get("my_sizes", {})
    feed_size = config.get("feed_size", 60)
    global_exclude = config.get("global_exclude", [])
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    seen_ids = load_json_set(SEEN_PATH)
    digest_pending = {}
    if DIGEST_PATH.exists():
        with open(DIGEST_PATH) as f:
            digest_pending = json.load(f)

    discovery_today = pick_discovery(config.get("discovery_pool", []), config.get("discovery_per_day", 3))
    scan_plan = [(w, "core") for w in config["watches"]] + [(w, "discovery") for w in discovery_today]

    session = new_session(domain)
    all_cards = []
    errors = []

    for watch, source in scan_plan:
        name = watch["name"]
        exclude_terms = global_exclude + watch.get("exclude", [])
        print(f"Checking ({source}): {name}")
        try:
            raw_items = run_search(session, domain, watch, currency, per_page)
        except requests.RequestException as e:
            print(f"  ! request failed: {e}")
            errors.append(name)
            continue

        new_watch_cards = []
        for item in raw_items:
            if not passes_filters(item, exclude_terms):
                continue
            if not brand_matches(item, watch):
                continue
            item_id = item.get("id")
            is_new = item_id not in seen_ids
            card = build_card(item, domain, watch, source, my_sizes, threshold_pct, max_price, is_new)
            all_cards.append(card)
            if is_new:
                seen_ids.add(item_id)
                new_watch_cards.append(card)

        if new_watch_cards and source == "core":
            notify_mode = watch.get("notify", "instant")
            print(f"  {len(new_watch_cards)} new")
            if notify_mode == "instant":
                notify_ntfy(ntfy_topic, f"Vinted: {len(new_watch_cards)} new — {name}", new_watch_cards)
            elif notify_mode == "digest":
                bucket = digest_pending.setdefault(name, [])
                existing = {c["id"] for c in bucket}
                bucket.extend(c for c in new_watch_cards if c["id"] not in existing)

        time.sleep(1)  # be polite between requests

    # De-dupe (a discovery brand can overlap a core search), rank, trim
    unique = {}
    for c in all_cards:
        existing = unique.get(c["id"])
        if existing is None or c["score"] > existing["score"]:
            unique[c["id"]] = c
    feed = sorted(unique.values(), key=lambda c: c["score"], reverse=True)[:feed_size]

    dashboard = {
        "generated_at": int(time.time()),
        "discovery_today": [w["name"] for w in discovery_today],
        "errors": errors,
        "feed": feed,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, sorted(seen_ids))
    save_json(DIGEST_PATH, digest_pending)
    print(f"Done. {len(feed)} listings in the feed.")


if __name__ == "__main__":
    main()
