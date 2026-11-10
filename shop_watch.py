#!/usr/bin/env python3
"""
Bargain Watch — Shopify retailer add-on
-----------------------------------------
Checks specific Shopify-based retailers (independent shops that happen to
run on Shopify, which exposes a public /products.json feed by default) for
genuine markdowns, using Shopify's own `compare_at_price` field rather than
any estimated RRP.

Separate script, separate data files (docs/shop_data.json and
shop_seen_ids.json), and its own GitHub Actions workflow (every 3 hours -
shop prices rarely change, so there is no reason to run this every 20
minutes alongside the Vinted scan). A problem here can never affect the
Vinted scan.

What it does on each run:
  1. Reads the WHOLE catalogue of each shop (paginates until a short page;
     a failed page keeps what was already fetched instead of dropping the
     shop; hitting the page cap is recorded in `errors`).
  2. Records the current price of EVERY in-scope product (on sale or not)
     in shop_seen_ids.json, storing change points only. That history is
     what lets us tell a real low from the shop's own "was" price.
  3. Emits one card per product that has a genuine markdown on an
     available size, scored mainly on verified signals (new all-time low,
     dropped since last scan, below its own typical price, how long we
     have been watching it) rather than the retailer's own discount.
  4. Flags "off-menu" finds: marked-down items that are live and buyable
     but not listed in the shop's Sale collections (the shop pulled them
     out of its menus). See `sale_collections` / `off_menu_tags` in
     config.json.

Usage:
  python shop_watch.py                         normal scan
  python shop_watch.py --list-sale-collections print each shop's sale-like
                                               collections (to fill in
                                               `sale_collections`)
"""

import json
import os
import re
import sys
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

PAGE_SIZE = 250
MAX_PAGES = 60            # 15,000 products per shop; hitting it is reported
COLLECTION_MAX_PAGES = 30
PAGE_DELAY = 1.0          # seconds between requests (set to 0 in tests)
# A "sale collection" with fewer products than this is treated as not
# really being the site's sale menu (e.g. Universal Works' /collections/sale
# returned 0 on 21 Oct 2026), so it can't be used to decide what is
# "off-menu" - tags are used instead.
MIN_SALE_COLLECTION_SIZE = 5
NEW_WINDOW_SECONDS = 24 * 3600
HISTORY_VERSION = 2


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _get_json(url, params):
    """One request with a single retry; raises requests.RequestException."""
    last = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * PAGE_DELAY)
    raise requests.RequestException(str(last))


def fetch_paginated(url, key, max_pages):
    """Page through a Shopify JSON endpoint until a short page.

    Returns (items, truncated, error):
      truncated - the page cap was hit while pages were still full
      error     - message if a page failed; items fetched before the
                  failure are still returned
    """
    items = []
    for page in range(1, max_pages + 1):
        try:
            data = _get_json(url, {"limit": PAGE_SIZE, "page": page})
        except requests.RequestException as e:
            return items, False, f"page {page} failed: {e}"
        batch = data.get(key, []) if isinstance(data, dict) else []
        if not batch:
            return items, False, None
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items, False, None
        if page == max_pages:
            return items, True, None
        time.sleep(PAGE_DELAY)
    return items, False, None


def fetch_products(domain, max_pages=None):
    return fetch_paginated(f"https://{domain}/products.json", "products", max_pages or MAX_PAGES)


def fetch_sale_handles(domain, collection_handles):
    """Product handles that appear in any of the shop's Sale collections.

    Returns a set, or None when it can't be trusted (nothing configured,
    all fetches failed, or the collections are suspiciously tiny).
    """
    if not collection_handles:
        return None
    handles = set()
    for ch in collection_handles:
        items, _truncated, err = fetch_paginated(
            f"https://{domain}/collections/{ch}/products.json", "products", COLLECTION_MAX_PAGES
        )
        if err:
            print(f"  ! sale collection '{ch}': {err}")
        print(f"  sale collection '{ch}': {len(items)} products")
        handles.update(p.get("handle") for p in items if p.get("handle"))
        time.sleep(PAGE_DELAY)
    if len(handles) < MIN_SALE_COLLECTION_SIZE:
        print(f"  ! sale collections only have {len(handles)} products - ignoring them for off-menu detection")
        return None
    return handles


# --------------------------------------------------------------------------
# Filtering helpers
# --------------------------------------------------------------------------

def parse_price(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


WOMENS_TERMS = ["women", "women's", "womens", "ladies", "lady's", "female"]


def product_tags(product):
    tags = product.get("tags")
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def is_womens_product(product):
    """Shopify's product feed has no standard men/women field the way
    Vinted's catalog_ids gave us - only title, product_type, and tags to
    go on. Checking all three catches most cases, though it's inherently
    less precise than Vinted's proper category filter."""
    fields = [(product.get("title") or "").lower(), (product.get("product_type") or "").lower()]
    fields.append(" ".join(product_tags(product)).lower())
    combined = " ".join(fields)
    return any(term in combined for term in WOMENS_TERMS)


def in_scope(product, global_exclude):
    title_lower = (product.get("title") or "").lower()
    if any(term.lower() in title_lower for term in global_exclude):
        return False
    return not is_womens_product(product)


def flatten_sizes(my_sizes_config):
    out = set()
    for sizes in (my_sizes_config or {}).values():
        for s in sizes:
            out.add(str(s).strip().lower())
    return out


def variant_matches_my_size(variant_title, my_sizes):
    """Variant titles look like "UK 10 / Chocolate Chip / Sport Yellow" or
    "W34 / L32" or just "M". Split on " / " and compare each part,
    case-insensitively, against the configured sizes."""
    if not variant_title or not my_sizes:
        return False
    return any(part.strip().lower() in my_sizes for part in variant_title.split(" / "))


# --------------------------------------------------------------------------
# Price history (version 2: change points only)
# --------------------------------------------------------------------------
# {"version": 2, "scan": N,
#  "items": {id: {"first_seen": unix|null, "first_scan": n,
#                 "low": x, "high": y, "prices": [[scan, price], ...]}}}
# A new [scan, price] pair is only added when the price changes, and the
# file is written one item per line with sorted keys, so an unchanged item
# produces no git diff.

def load_history(path):
    """Returns (history, bootstrap). Migrates the old {id: price} format."""
    if not path.exists():
        return {"version": HISTORY_VERSION, "scan": 0, "items": {}}, True
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get("version") == HISTORY_VERSION:
        return data, False
    # Old format: {id: last_price}. Treat each as a first observation at scan 1.
    items = {}
    for item_id, price in data.items():
        p = parse_price(price)
        if p is None:
            continue
        items[item_id] = {"first_seen": None, "first_scan": 1, "low": p, "high": p, "prices": [[1, p]]}
    print(f"Migrated {len(items)} items from the old price-history format")
    return {"version": HISTORY_VERSION, "scan": 1, "items": items}, False


def save_history(path, history):
    lines = []
    for item_id in sorted(history["items"]):
        lines.append(json.dumps(item_id) + ":" + json.dumps(history["items"][item_id], sort_keys=True, separators=(",", ":")))
    body = ",\n".join(lines)
    with open(path, "w") as f:
        f.write('{"version":%d,"scan":%d,"items":{\n%s\n}}\n' % (history["version"], history["scan"], body))


def record_price(history, item_id, price, scan, now, bootstrap):
    """Add today's observation and return the item's history entry."""
    entry = history["items"].get(item_id)
    if entry is None:
        entry = {
            "first_seen": None if bootstrap else now,
            "first_scan": scan,
            "low": price,
            "high": price,
            "prices": [[scan, price]],
        }
        history["items"][item_id] = entry
        return entry
    if abs(entry["prices"][-1][1] - price) > 0.005:
        entry["prices"].append([scan, price])
        entry["low"] = min(entry["low"], price)
        entry["high"] = max(entry["high"], price)
    return entry


def derive(entry, scan, now):
    """Verified signals worked out from the stored change points."""
    prices = entry["prices"]
    price = prices[-1][1]
    first_scan = entry["first_scan"]
    observations = max(1, scan - first_scan + 1)

    # Time-weighted average over every scan since first seen.
    total = 0.0
    for i, (s, p) in enumerate(prices):
        end = prices[i + 1][0] if i + 1 < len(prices) else scan + 1
        total += p * max(0, end - s)
    span = max(1, scan + 1 - prices[0][0])
    typical = total / span

    moved = len(prices) > 1
    prior_low = min(p for _, p in prices[:-1]) if moved else None
    at_low = bool(moved and price <= prior_low + 0.005)

    price_dropped = False
    drop_pct = None
    previous_price = None
    if moved and prices[-1][0] == scan:
        previous_price = prices[-2][1]
        drop = previous_price - price
        if drop > 0.5 and previous_price > 0 and drop / previous_price > 0.01:
            price_dropped = True
            drop_pct = round(drop / previous_price * 100)

    first_seen = entry.get("first_seen")
    is_new = first_seen is not None and (now - first_seen) < NEW_WINDOW_SECONDS

    return {
        "price": price,
        "observations": observations,
        "typical": typical,
        "at_low": at_low,
        "price_dropped": price_dropped,
        "price_drop_pct": drop_pct,
        "previous_price": previous_price if price_dropped else None,
        "is_new": is_new,
    }


def score_card(discount_pct, d, off_menu):
    score = min(discount_pct, 60) * 0.35
    if d["at_low"]:
        score += 45
    if d["price_dropped"]:
        score += 20
    if d["typical"] > 0 and d["price"] < d["typical"]:
        below = (d["typical"] - d["price"]) / d["typical"] * 100
        score += min(below * 0.4, 50)
    score += min(d["observations"] * 0.25, 40)
    if d["is_new"]:
        score += 3
    if off_menu:
        score += 10
    return round(score, 1)


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------

def current_price(product):
    """The price used for history: cheapest available variant (falling back
    to the cheapest of all variants if none are flagged available)."""
    variants = product.get("variants", [])
    avail = [parse_price(v.get("price")) for v in variants if v.get("available") is not False]
    avail = [p for p in avail if p is not None]
    if avail:
        return min(avail)
    allp = [parse_price(v.get("price")) for v in variants]
    allp = [p for p in allp if p is not None]
    return min(allp) if allp else None


def build_card(product, domain, shop, entry, scan, now, my_sizes, sale_handles, currency="GBP"):
    """One card per product with a genuine markdown on an available size."""
    on_sale_variants = []
    for variant in product.get("variants", []):
        price = parse_price(variant.get("price"))
        compare_at = parse_price(variant.get("compare_at_price"))
        # Only a genuine markdown counts - Shopify sometimes sets
        # compare_at_price equal to price, which isn't actually a sale.
        if price is None or compare_at is None or compare_at <= price:
            continue
        # A discounted price means nothing if the size is sold out. Only an
        # explicit False excludes it, since some stores omit the field.
        if variant.get("available") is False:
            continue
        on_sale_variants.append((variant, price, compare_at))
    if not on_sale_variants:
        return None

    _best, price, compare_at = min(on_sale_variants, key=lambda v: v[1])
    discount_pct = round((1 - price / compare_at) * 100)

    sizes = []
    my_size = False
    for variant, _p, _c in on_sale_variants:
        vt = variant.get("title", "")
        if vt and vt != "Default Title" and vt not in sizes:
            sizes.append(vt)
        if variant_matches_my_size(vt, my_sizes):
            my_size = True

    title = (product.get("title") or "").strip()
    handle = product.get("handle", "")
    images = product.get("images") or []
    photo = images[0].get("src", "") if images else ""

    off_menu = False
    if sale_handles is not None and handle and handle not in sale_handles:
        off_menu = True
    shop_tags = {t.lower() for t in shop.get("off_menu_tags", [])}
    if shop_tags and any(t.lower() in shop_tags for t in product_tags(product)):
        off_menu = True

    d = derive(entry, scan, now)
    # The history price is the cheapest available variant; a card is only
    # built from on-sale variants, so show the cheapest of those.
    d["price"] = price if abs(price - d["price"]) > 0.005 else d["price"]
    was_price_seen = bool(len(entry["prices"]) > 1 and entry["high"] >= compare_at - 0.01)
    score = score_card(discount_pct, d, off_menu)

    return {
        "id": f"shop-{domain}-{product.get('id')}",
        "title": title,
        "watch": shop["name"],
        "brand": (product.get("vendor") or "").strip(),
        "size": ", ".join(sizes),
        "condition": "New",
        "price": f"{price:.2f} {currency}",
        "price_amount": price,
        "estimated_total": None,  # direct retail - no buyer protection fee
        "photo": photo,
        "url": f"https://{domain}/products/{handle}",
        "seller": {"login": shop["name"], "profile_url": "", "business": True},
        "discount_pct": discount_pct,
        "score": score,
        "my_size": my_size,
        "is_new": d["is_new"],
        "is_bargain": discount_pct >= 30,
        "bargain_reason": f"-{discount_pct}%",
        "source": "core",
        "listed_at": None,
        "condition_badge": "NEW",
        "category": "electronics",
        "subcategory": "shops",
        "price_dropped": d["price_dropped"],
        "price_drop_pct": d["price_drop_pct"],
        "previous_price": d["previous_price"],
        "marketplace": "shop",
        # new fields
        "compare_at_price": compare_at,
        "at_low": d["at_low"],
        "was_price_seen": was_price_seen,
        "observations": d["observations"],
        "typical_price": round(d["typical"], 2),
        "off_menu": off_menu,
    }


def family_key(card):
    title = card["title"]
    if " - " in title:
        title = title.split(" - ")[0]
    return (card["watch"], title.strip().lower())


def dedupe_families(cards):
    """Same product in several colours ("Name - Navy", "Name - Olive")
    should take one slot, not several: keep the best-scoring one."""
    best = {}
    for c in cards:
        k = family_key(c)
        if k not in best or c["score"] > best[k]["score"]:
            best[k] = c
    return list(best.values())


def build_feed(all_cards, feed_size=60, min_per_shop=5, max_per_brand=2, single_brand_shops=(), max_per_shop=None):
    """Reserve a minimum number of slots per shop (so small specialists stay
    visible), then fill the rest by score. At most `max_per_brand` items per
    brand within a shop, so one brand's clearance can't fill the feed -
    except in single-brand shops (e.g. Universal Works), where the brand
    cap would just cap the whole shop."""
    brand_counts = {}
    shop_counts = {}

    def brand_ok(c):
        if max_per_shop and shop_counts.get(c["watch"], 0) >= max_per_shop:
            return False
        if not max_per_brand or c["watch"] in single_brand_shops:
            return True
        return brand_counts.get((c["watch"], (c["brand"] or "").lower()), 0) < max_per_brand

    def take(c):
        k = (c["watch"], (c["brand"] or "").lower())
        brand_counts[k] = brand_counts.get(k, 0) + 1
        shop_counts[c["watch"]] = shop_counts.get(c["watch"], 0) + 1

    by_shop = {}
    for c in all_cards:
        by_shop.setdefault(c["watch"], []).append(c)
    for shop in by_shop:
        by_shop[shop].sort(key=lambda c: c["score"], reverse=True)

    feed = []
    seen_ids = set()
    for shop, cards in by_shop.items():
        n = 0
        for c in cards:
            if n >= min_per_shop:
                break
            if brand_ok(c):
                feed.append(c)
                seen_ids.add(c["id"])
                take(c)
                n += 1

    remaining = sorted((c for c in all_cards if c["id"] not in seen_ids), key=lambda c: c["score"], reverse=True)
    for c in remaining:
        if len(feed) >= feed_size:
            break
        if brand_ok(c):
            feed.append(c)
            take(c)

    feed.sort(key=lambda c: c["score"], reverse=True)
    return feed[:feed_size]


# --------------------------------------------------------------------------
# Optional ntfy alert
# --------------------------------------------------------------------------

def notify_ntfy(topic, cards):
    if not topic or not cards:
        return
    lines = [f"{c['title']} - £{c['price_amount']:.2f} (was £{c['compare_at_price']:.2f}) @ {c['watch']}" for c in cards[:5]]
    message = "\n".join(lines)
    if len(cards) > 5:
        message += f"\n…and {len(cards) - 5} more"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": "Off-menu find in your size".encode("utf-8"), "Priority": "high", "Tags": "shirt,mag"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  ! ntfy push failed: {e}")


# --------------------------------------------------------------------------
# Helper: discover which collections to use as `sale_collections`
# --------------------------------------------------------------------------

SALE_WORDS = re.compile(r"sale|clearance|outlet|archive|last|reduced|markdown|end.of|offer|seconds|bargain", re.I)


def list_sale_collections(shop_watches):
    for shop in shop_watches:
        domain = shop["domain"]
        print(f"\n{shop['name']} ({domain})")
        cols, _t, err = fetch_paginated(f"https://{domain}/collections.json", "collections", 10)
        if err:
            print(f"  ! {err}")
        for c in cols:
            label = f"{c.get('handle', '')} {c.get('title', '')}"
            if SALE_WORDS.search(label):
                print(f"  {c.get('handle'):40s} {c.get('products_count', '?'):>5}  {c.get('title')}")
        time.sleep(PAGE_DELAY)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    shop_watches = config.get("shop_watches", [])
    if not shop_watches:
        print("No shop_watches configured in config.json - nothing to do.")
        return

    if "--list-sale-collections" in sys.argv:
        list_sale_collections(shop_watches)
        return

    global_exclude = config.get("global_exclude", [])
    my_sizes = flatten_sizes(config.get("my_sizes"))
    feed_size = config.get("shop_feed_size", 60)
    max_per_brand = config.get("shop_max_per_brand", 2)
    max_per_shop = config.get("shop_max_per_shop", 20)

    history, bootstrap = load_history(SEEN_PATH)
    scan = history["scan"] + 1
    history["scan"] = scan
    now = int(time.time())
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    all_cards = []
    errors = []
    single_brand_shops = set()

    for shop in shop_watches:
        name = shop["name"]
        domain = shop["domain"]
        currency = shop.get("currency", "GBP")
        print(f"Checking (shop): {name} ({domain})")

        products, truncated, err = fetch_products(domain)
        print(f"  {len(products)} products fetched")
        if err:
            print(f"  ! {err} - keeping the {len(products)} products already fetched")
            errors.append(f"{name} (partial: {err})" if products else name)
        if truncated:
            print(f"  ! catalogue truncated at {MAX_PAGES} pages")
            errors.append(f"{name} (catalogue truncated)")
        if not products:
            if not err:
                errors.append(name)
            continue

        sale_handles = fetch_sale_handles(domain, shop.get("sale_collections", []))

        prefix = f"shop-{domain}-"
        known_before = [k for k in history["items"] if k.startswith(prefix)]
        present = set()
        vendors = set()
        shop_cards = []
        marked_down = 0
        skipped = 0
        for product in products:
            try:
                if not in_scope(product, global_exclude):
                    continue
                price = current_price(product)
                if price is None:
                    continue
                item_id = f"{prefix}{product.get('id')}"
                present.add(item_id)
                vendors.add((product.get("vendor") or "").strip().lower())
                entry = record_price(history, item_id, price, scan, now, bootstrap)
                card = build_card(product, domain, shop, entry, scan, now, my_sizes, sale_handles, currency)
                if card:
                    marked_down += 1
                    shop_cards.append(card)
            except Exception as e:  # one malformed product must not sink the shop
                skipped += 1
                print(f"  ! skipped one malformed product: {e}")

        if len(vendors) <= 2:
            single_brand_shops.add(name)

        # Forget products that have left a shop - but only after a complete,
        # healthy read of that shop, so a flaky response can't wipe history.
        if not err and not truncated and known_before and len(present) >= 0.8 * len(known_before):
            for k in known_before:
                if k not in present:
                    del history["items"][k]

        shop_cards = dedupe_families(shop_cards)
        off = sum(1 for c in shop_cards if c["off_menu"])
        print(f"  {len(present)} in scope, {marked_down} marked down, {len(shop_cards)} cards after colour-dedupe"
              f"{f', {off} off-menu' if sale_handles is not None or shop.get('off_menu_tags') else ''}")
        if skipped:
            print(f"  {skipped} product(s) skipped due to unexpected data")
        all_cards.extend(shop_cards)
        time.sleep(PAGE_DELAY)

    feed = build_feed(all_cards, feed_size=feed_size, min_per_shop=5,
                      max_per_brand=max_per_brand, single_brand_shops=single_brand_shops,
                      max_per_shop=max_per_shop)

    # One-off alert for off-menu + your size + at its lowest. `alerted` is
    # stored on the item's history line so the same item only pings once.
    alert = []
    for c in feed:
        entry = history["items"].get(c["id"])
        if entry and c["off_menu"] and c["my_size"] and c["at_low"] and not entry.get("alerted"):
            alert.append(c)
            entry["alerted"] = True
    notify_ntfy(ntfy_topic, alert)

    dashboard = {"generated_at": now, "errors": errors, "feed": feed}
    DOCS_DIR.mkdir(exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(dashboard, f, indent=2)
    save_history(SEEN_PATH, history)
    print(f"Done. {len(feed)} shop listings written, history now {len(history['items'])} items (scan #{scan}).")


if __name__ == "__main__":
    main()
