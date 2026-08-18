#!/usr/bin/env python3
"""
Bargain Watch — Shopify retailer add-on
-----------------------------------------
Checks specific Shopify-based retailers (independent shops that happen to
run on Shopify, which exposes a public /products.json feed by default) for
genuine markdowns, using Shopify's own `compare_at_price` field rather than
any estimated RRP.

This is the most reliable price signal anywhere in this project - Shopify
tells you the real "was" price directly, no guessing required. The
trade-off is it only works for shops actually on Shopify - not every
retailer is, and this only covers whichever shops are listed in
`shop_watches` in config.json.

Separate script, separate data file (docs/shop_data.json), same reasoning
as ebay_watch.py: a problem here can never affect the Vinted scan.
"""

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"
DATA_PATH = DOCS_DIR / "shop_data.json"
SEEN_PATH = ROOT / "shop_seen_ids.json"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def load_price_history(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def fetch_products(domain, max_pages=5):
    """Shopify's public product feed, paginated 250 at a time."""
    products = []
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json"
        resp = requests.get(
            url, params={"limit": 250, "page": page}, headers=REQUEST_HEADERS, timeout=20
        )
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:
            break
        time.sleep(1)
    return products


def parse_price(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_cards(product, domain, shop_name, global_exclude, price_history):
    title = (product.get("title") or "").strip()
    title_lower = title.lower()
    if any(term.lower() in title_lower for term in global_exclude):
        return []

    vendor = (product.get("vendor") or "").strip()
    handle = product.get("handle", "")
    images = product.get("images") or []
    photo = images[0].get("src", "") if images else ""
    product_url = f"https://{domain}/products/{handle}"

    cards = []
    for variant in product.get("variants", []):
        price = parse_price(variant.get("price"))
        compare_at = parse_price(variant.get("compare_at_price"))

        # Only a genuine markdown counts - Shopify sometimes sets
        # compare_at_price equal to price, which isn't actually a sale.
        if price is None or compare_at is None or compare_at <= price:
            continue

        discount_pct = round((1 - price / compare_at) * 100)
        variant_id = variant.get("id")
        item_id = f"shop-{domain}-{variant_id}"
        is_new = item_id not in price_history
        previous_price = price_history.get(item_id)

        price_dropped = False
        price_drop_pct = None
        if previous_price is not None and previous_price > 0:
            drop = previous_price - price
            if drop > 0.5 and drop / previous_price > 0.01:
                price_dropped = True
                price_drop_pct = round((drop / previous_price) * 100)

        variant_title = variant.get("title", "")
        size = "" if variant_title == "Default Title" else variant_title

        score = min(discount_pct, 70)
        if price_dropped:
            score += 15
        if is_new:
            score += 5

        price_history[item_id] = price

        cards.append({
            "id": item_id,
            "title": title,
            "watch": shop_name,
            "brand": vendor,
            "size": size,
            "condition": "New",
            "price": f"{price:.2f} GBP",
            "price_amount": price,
            "estimated_total": price,  # retail price, no marketplace fee to add
            "photo": photo,
            "url": product_url,
            "seller": {"login": shop_name, "feedback_count": 0, "reputation": None},
            "discount_pct": discount_pct,
            "score": score,
            "my_size": False,
            "is_new": is_new,
            "is_bargain": discount_pct >= 30,
            "bargain_reason": f"-{discount_pct}%",
            "source": "core",
            "listed_at": None,
            "condition_badge": "NEW",
            "category": "electronics",
            "subcategory": "shops",
            "price_dropped": price_dropped,
            "price_drop_pct": price_drop_pct,
            "previous_price": previous_price if price_dropped else None,
            "marketplace": "shop",
        })
    return cards


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    shop_watches = config.get("shop_watches", [])
    if not shop_watches:
        print("No shop_watches configured in config.json - nothing to do.")
        return

    global_exclude = config.get("global_exclude", [])
    price_history = load_price_history(SEEN_PATH)
    all_cards = []
    errors = []

    for shop in shop_watches:
        name = shop["name"]
        domain = shop["domain"]
        print(f"Checking (shop): {name} ({domain})")
        try:
            products = fetch_products(domain)
        except requests.RequestException as e:
            print(f"  ! request failed: {e}")
            errors.append(name)
            continue
        print(f"  {len(products)} products fetched")

        shop_cards = []
        skipped = 0
        for product in products:
            try:
                shop_cards.extend(build_cards(product, domain, name, global_exclude, price_history))
            except Exception as e:
                skipped += 1
                print(f"  ! skipped one malformed product: {e}")
                continue
        print(f"  {len(shop_cards)} on genuine markdown")
        if skipped:
            print(f"  {skipped} product(s) skipped due to unexpected data")
        all_cards.extend(shop_cards)
        time.sleep(1)

    dashboard = {
        "generated_at": int(time.time()),
        "errors": errors,
        "feed": sorted(all_cards, key=lambda c: c["score"], reverse=True)[:60],
    }
    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, price_history)
    print(f"Done. {len(dashboard['feed'])} shop listings written.")


if __name__ == "__main__":
    main()
