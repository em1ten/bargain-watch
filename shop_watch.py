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


WOMENS_TERMS = ["women", "women's", "womens", "ladies", "lady's", "female"]


def is_womens_product(product):
    """Shopify's product feed has no standard men/women field the way
    Vinted's catalog_ids gave us - only title, product_type, and tags to
    go on. Checking all three catches most cases, though it's inherently
    less precise than Vinted's proper category filter."""
    fields = [(product.get("title") or "").lower(), (product.get("product_type") or "").lower()]
    tags = product.get("tags")
    if isinstance(tags, str):
        fields.append(tags.lower())
    elif isinstance(tags, list):
        fields.append(" ".join(str(t) for t in tags).lower())
    combined = " ".join(fields)
    return any(term in combined for term in WOMENS_TERMS)


def build_cards(product, domain, shop_name, global_exclude, price_history):
    title = (product.get("title") or "").strip()
    title_lower = title.lower()
    if any(term.lower() in title_lower for term in global_exclude):
        return []
    if is_womens_product(product):
        return []

    vendor = (product.get("vendor") or "").strip()
    handle = product.get("handle", "")
    images = product.get("images") or []
    photo = images[0].get("src", "") if images else ""
    product_url = f"https://{domain}/products/{handle}"

    # A Shopify "product" is one item with size variants - unlike Vinted,
    # where every listing genuinely is unique. Collect every variant with a
    # real markdown, then emit ONE card for the whole product (using the
    # cheapest marked-down price for scoring), listing all its on-sale
    # sizes together - not one duplicate card per size.
    on_sale_variants = []
    for variant in product.get("variants", []):
        price = parse_price(variant.get("price"))
        compare_at = parse_price(variant.get("compare_at_price"))
        # Only a genuine markdown counts - Shopify sometimes sets
        # compare_at_price equal to price, which isn't actually a sale.
        if price is None or compare_at is None or compare_at <= price:
            continue
        on_sale_variants.append((variant, price, compare_at))

    if not on_sale_variants:
        return []

    best_variant, price, compare_at = min(on_sale_variants, key=lambda v: v[1])
    discount_pct = round((1 - price / compare_at) * 100)

    sizes = []
    for variant, v_price, _ in on_sale_variants:
        variant_title = variant.get("title", "")
        if variant_title and variant_title != "Default Title" and variant_title not in sizes:
            sizes.append(variant_title)
    size = ", ".join(sizes)

    product_id = product.get("id")
    item_id = f"shop-{domain}-{product_id}"
    is_new = item_id not in price_history
    previous_price = price_history.get(item_id)

    price_dropped = False
    price_drop_pct = None
    if previous_price is not None and previous_price > 0:
        drop = previous_price - price
        if drop > 0.5 and drop / previous_price > 0.01:
            price_dropped = True
            price_drop_pct = round((drop / previous_price) * 100)

    score = min(discount_pct, 70)
    if price_dropped:
        score += 15
    if is_new:
        score += 5

    price_history[item_id] = price

    return [{
        "id": item_id,
        "title": title,
        "watch": shop_name,
        "brand": vendor,
        "size": size,
        "condition": "New",
        "price": f"{price:.2f} GBP",
        "price_amount": price,
        "estimated_total": None,  # direct retail - no buyer protection fee, so no fee note to show
        "photo": photo,
        "url": product_url,
        "seller": {"login": shop_name, "profile_url": "", "business": True},
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
    }]


def build_feed(all_cards, feed_size=60, min_per_shop=5):
    """Simple score-sorting lets one high-volume shop (e.g. Universal
    Works, which carries its whole current range) fill every slot and
    crowd out smaller specialists that only have a handful of genuinely
    great markdowns. Reserve a minimum number of slots per shop first -
    guaranteeing every shop stays visible - then fill whatever's left
    with the best remaining cards regardless of shop, so a
    high-volume shop can still dominate the "extra" space if it
    genuinely earns it."""
    by_shop = {}
    for c in all_cards:
        by_shop.setdefault(c["watch"], []).append(c)
    for shop in by_shop:
        by_shop[shop].sort(key=lambda c: c["score"], reverse=True)

    feed = []
    seen_ids = set()
    for shop, cards in by_shop.items():
        for c in cards[:min_per_shop]:
            feed.append(c)
            seen_ids.add(c["id"])

    remaining = [c for c in all_cards if c["id"] not in seen_ids]
    remaining.sort(key=lambda c: c["score"], reverse=True)
    feed.extend(remaining[: max(0, feed_size - len(feed))])

    feed.sort(key=lambda c: c["score"], reverse=True)
    return feed[:feed_size]


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
        "feed": build_feed(all_cards, feed_size=60, min_per_shop=5),
    }
    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, price_history)
    print(f"Done. {len(dashboard['feed'])} shop listings written.")


if __name__ == "__main__":
    main()
