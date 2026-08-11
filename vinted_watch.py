#!/usr/bin/env python3
"""
Vinted Watch
------------
Checks a list of saved searches against Vinted's public search endpoint,
tracks which listings have already been seen, writes a snapshot for the
dashboard (docs/data.json), and sends notifications via ntfy.sh — either
straight away ("instant" watches) or queued up for the once-daily digest
("digest" watches, sent by digest_send.py).

This talks to the same endpoint the Vinted website itself uses when you
search. It's not an official/public API, so if Vinted changes something,
this script may need small tweaks (that's normal for tools like this).

Usage:
    python3 vinted_watch.py

Config (config.json):
    watches            - list of searches, each with an optional "notify"
                          of "instant", "digest", or "off"
    global_exclude      - keywords filtered out of every search's results
    conditions          - allowed condition labels (leave empty/omit for any)

Env:
    NTFY_TOPIC (env)   - optional. If set, "instant" watches push straight
                         to https://ntfy.sh/<topic>. Leave unset to skip
                         notifications and just update the dashboard.
"""

import json
import os
import time
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
    payload = resp.json()
    return payload.get("items", [])


def passes_filters(item, exclude_terms, allowed_conditions):
    title = (item.get("title") or "").lower()
    if any(term.lower() in title for term in exclude_terms):
        return False
    if allowed_conditions:
        status = (item.get("status") or "").strip()
        if status and status not in allowed_conditions:
            return False
    return True


def to_card(item, domain):
    photo = (item.get("photo") or {}).get("url", "")
    price_obj = item.get("total_item_price") or item.get("price") or {}
    amount = price_obj.get("amount")
    currency = price_obj.get("currency_code", "")

    user = item.get("user") or {}
    seller = {
        "login": user.get("login", ""),
        "feedback_count": user.get("feedback_count") or user.get("positive_feedback_count") or 0,
        "reputation": user.get("feedback_reputation"),  # 0.0-1.0 if present
    }

    return {
        "id": item.get("id"),
        "title": item.get("title", "").strip(),
        "brand": (item.get("brand_title") or "").strip(),
        "size": (item.get("size_title") or "").strip(),
        "condition": (item.get("status") or "").strip(),
        "price": f"{amount} {currency}" if amount else "",
        "photo": photo,
        "url": f"https://{domain}/items/{item.get('id')}",
        "seller": seller,
    }


def notify_ntfy(topic, title, cards):
    if not topic or not cards:
        return
    lines = [f"{c['brand'] or ''} — {c['price']}: {c['title']}".strip(" —") for c in cards[:5]]
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
    global_exclude = config.get("global_exclude", [])
    allowed_conditions = set(config.get("conditions", []))
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    seen_ids = load_json_set(SEEN_PATH)
    digest_pending = {}
    if DIGEST_PATH.exists():
        with open(DIGEST_PATH) as f:
            digest_pending = json.load(f)

    session = new_session(domain)

    dashboard = {"generated_at": int(time.time()), "watches": []}

    for watch in config["watches"]:
        name = watch["name"]
        notify_mode = watch.get("notify", "instant")
        exclude_terms = global_exclude + watch.get("exclude", [])

        print(f"Checking: {name}")
        try:
            raw_items = run_search(session, domain, watch, currency, per_page)
        except requests.RequestException as e:
            print(f"  ! request failed: {e}")
            dashboard["watches"].append({"name": name, "error": str(e), "items": []})
            continue

        filtered = [i for i in raw_items if passes_filters(i, exclude_terms, allowed_conditions)]
        cards = [to_card(item, domain) for item in filtered]
        new_cards = [c for c in cards if c["id"] not in seen_ids]

        for c in cards:
            seen_ids.add(c["id"])
            c["is_new"] = c in new_cards

        dashboard["watches"].append({"name": name, "items": cards, "new_count": len(new_cards)})

        if new_cards:
            print(f"  {len(new_cards)} new")
            if notify_mode == "instant":
                notify_ntfy(ntfy_topic, f"Vinted: {len(new_cards)} new — {name}", new_cards)
            elif notify_mode == "digest":
                bucket = digest_pending.setdefault(name, [])
                existing_ids = {c["id"] for c in bucket}
                for c in new_cards:
                    if c["id"] not in existing_ids:
                        bucket.append(c)
            # "off" -> no notification, still shown on dashboard

        time.sleep(1)  # be polite between requests

    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, sorted(seen_ids))
    save_json(DIGEST_PATH, digest_pending)

    print("Done. Dashboard data written to docs/data.json")


if __name__ == "__main__":
    main()
