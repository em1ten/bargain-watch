#!/usr/bin/env python3
"""
Bargain Watch — eBay add-on
----------------------------
Scans eBay's Browse API (client-credentials app auth, no user login) for
the Technology/Games/Music watches and writes docs/ebay_data.json, which
the dashboard fetches alongside the main Vinted data.json and merges in.

Deliberately a separate script and a separate data file from
vinted_watch.py - eBay's API, auth, and response shape are all completely
different, and keeping it decoupled means a problem here can never break
the working Vinted scan.

Honest limitation: eBay deprecated sold-price access (the old Finding API)
in 2025. The Browse API used here only sees *active* listings, not sold
prices - so like Vinted, discount-vs-RRP still relies on the rough `rrp`
estimates in config.json, not real recent sale data.

Requires two repo secrets: EBAY_CLIENT_ID and EBAY_CLIENT_SECRET, from an
eBay Developer account (Production keyset). If either is missing, this
script exits quietly rather than failing the whole workflow - the Vinted
scan still runs fine without it.
"""

import base64
import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"
DATA_PATH = DOCS_DIR / "ebay_data.json"
SEEN_PATH = ROOT / "ebay_seen_ids.json"

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_GB"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay's condition strings vary by category and aren't as clean as Vinted's -
# best-effort mapping, worth refining once real responses are seen.
CONDITION_SCORES = {
    "New": 30,
    "New other (see details)": 24,
    "New with defects": 15,
    "Certified - Refurbished": 20,
    "Seller refurbished": 16,
    "Like New": 22,
    "Very Good": 12,
    "Good": 8,
    "Acceptable": 4,
    "Used": 6,
    "For parts or not working": 0,
}


def load_price_history(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def get_access_token(client_id, client_secret):
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def run_search(token, watch):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    params = {"q": watch["search_text"], "category_ids": watch["category_id"], "limit": 50}
    filters = []
    price_to = watch.get("price_to")
    if price_to:
        filters.append(f"price:[..{price_to}]")
        filters.append("priceCurrency:GBP")
    if filters:
        params["filter"] = ",".join(filters)
    resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("itemSummaries", [])


def build_card(item, watch, is_new, previous_price):
    price_obj = item.get("price") or {}
    amount = price_obj.get("value")
    currency = price_obj.get("currency", "GBP")
    price_amount = None
    if amount is not None:
        try:
            price_amount = float(amount)
        except (TypeError, ValueError):
            pass

    condition = item.get("condition", "")
    photo = (item.get("image") or {}).get("imageUrl", "")
    title = item.get("title", "")
    url = item.get("itemWebUrl", "")

    seller_raw = item.get("seller") or {}
    feedback_pct = seller_raw.get("feedbackPercentage")
    reputation = None
    if feedback_pct is not None:
        try:
            reputation = float(feedback_pct) / 100
        except (TypeError, ValueError):
            pass
    seller = {
        "login": seller_raw.get("username", ""),
        "feedback_count": seller_raw.get("feedbackScore", 0) or 0,
        "reputation": reputation,
    }

    rrp = watch.get("rrp")
    discount_pct = None
    if rrp and price_amount is not None and rrp > 0 and price_amount >= rrp * 0.15:
        discount_pct = round((1 - price_amount / rrp) * 100)

    max_price = watch.get("bargain_max_price")
    threshold_pct = watch.get("bargain_threshold_pct", 50)

    price_dropped = False
    price_drop_pct = None
    if previous_price is not None and price_amount is not None and previous_price > 0:
        drop = previous_price - price_amount
        if drop > 0.5 and drop / previous_price > 0.01:
            price_dropped = True
            price_drop_pct = round((drop / previous_price) * 100)

    score = 0
    if discount_pct is not None and discount_pct > 0:
        score += min(discount_pct, 60)
    if max_price is not None and price_amount is not None and price_amount <= max_price:
        score += 15
    score += CONDITION_SCORES.get(condition, 5)
    if isinstance(reputation, (int, float)) and reputation >= 0.95 and seller["feedback_count"] >= 10:
        score += 5
    if is_new:
        score += 5
    if price_dropped:
        score += 15

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
        "id": f"ebay-{item.get('itemId')}",
        "title": title.strip(),
        "watch": watch["name"],
        "brand": (item.get("brand") or "").strip(),
        "size": "",
        "condition": condition,
        "price": f"{amount} {currency}" if amount else "",
        "price_amount": price_amount,
        "estimated_total": price_amount,  # eBay UK has no separate buyer-protection fee like Vinted's
        "photo": photo,
        "url": url,
        "seller": seller,
        "discount_pct": discount_pct,
        "score": score,
        "my_size": False,
        "is_new": is_new,
        "is_bargain": is_bargain,
        "bargain_reason": bargain_reason,
        "source": "core",
        "listed_at": None,
        "condition_badge": None,
        "category": "electronics",
        "subcategory": watch.get("subcategory"),
        "price_dropped": price_dropped,
        "price_drop_pct": price_drop_pct,
        "previous_price": previous_price if price_dropped else None,
        "marketplace": "ebay",
    }


def main():
    client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - skipping eBay scan.")
        return

    with open(CONFIG_PATH) as f:
        config = json.load(f)
    ebay_watches = config.get("ebay_watches", [])
    if not ebay_watches:
        print("No ebay_watches configured in config.json - nothing to do.")
        return

    try:
        token = get_access_token(client_id, client_secret)
    except requests.RequestException as e:
        print(f"! Couldn't get eBay access token: {e}")
        return

    price_history = load_price_history(SEEN_PATH)
    all_cards = []
    errors = []

    for watch in ebay_watches:
        name = watch["name"]
        print(f"Checking (eBay): {name}")
        try:
            items = run_search(token, watch)
        except requests.RequestException as e:
            print(f"  ! request failed: {e}")
            errors.append(name)
            continue
        print(f"  {len(items)} raw results from eBay")

        skipped = 0
        for item in items:
            try:
                item_id = f"ebay-{item.get('itemId')}"
                is_new = item_id not in price_history
                previous_price = price_history.get(item_id)
                card = build_card(item, watch, is_new, previous_price)
            except Exception as e:
                skipped += 1
                print(f"  ! skipped one malformed item: {e}")
                continue
            all_cards.append(card)
            price_history[item_id] = card["price_amount"]
        if skipped:
            print(f"  {skipped} item(s) skipped due to unexpected data")

        time.sleep(1)  # be polite between requests

    dashboard = {
        "generated_at": int(time.time()),
        "errors": errors,
        "feed": sorted(all_cards, key=lambda c: c["score"], reverse=True)[:60],
    }
    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, price_history)
    print(f"Done. {len(dashboard['feed'])} eBay listings written.")


if __name__ == "__main__":
    main()
