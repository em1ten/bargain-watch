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

Separate script, separate data file (docs/shop_data.json), deliberately
decoupled: a problem here can never affect the Vinted scan.
"""

import json
import re
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
        if page == max_pages:
            # A full final page means the catalogue continues past our cap -
            # anything beyond it is invisible to the scan. Say so in the log
            # rather than silently missing sale items; raise max_pages if
            # this shows up for a shop that matters (costs ~1s per page).
            print(f"  ! catalogue truncated at {max_pages} pages ({len(products)} products) - some items not scanned")
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


# Shops often spell sizes out ("Medium") where Vinted uses letters ("M").
# Matched only as a WHOLE segment of the variant title (split on '/' and
# ','), never as a loose word - otherwise a colour like "Medium Blue"
# would wrongly match a size term.
WORD_SIZES = {
    "s": "small",
    "m": "medium",
    "l": "large",
    "xl": "x-large",
}


def size_matches(size_title, size_terms):
    """Match a size term as a distinct token within the variant's size
    string, not as a raw substring - same regex as vinted_watch.py, so
    'L' doesn't wrongly match inside 'XL', and '.'/',' count as part of a
    size token so '9' doesn't wrongly match inside '9.5'."""
    if not size_terms:
        return False
    size = (size_title or "").strip().lower()
    if not size:
        return False
    segments = [seg.strip() for seg in re.split(r"[/,]", size)]
    for term in size_terms:
        t = term.strip().lower()
        if not t:
            continue
        pattern = r"(?<![a-z0-9.,])" + re.escape(t) + r"(?![a-z0-9.,])"
        if re.search(pattern, size):
            return True
        word = WORD_SIZES.get(t)
        if word and any(seg == word or seg == word.replace("-", " ") for seg in segments):
            return True
    return False


def build_cards(product, domain, shop_name, global_exclude, price_history, size_terms=None):
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
        # Sold-out variants stay in the feed with their old markdown, so
        # without this check a card can list sizes that are actually
        # greyed out / "notify me" on the shop. Only treat an explicit
        # False as sold out - if a shop's feed ever omits the field,
        # everything still shows rather than everything vanishing.
        if variant.get("available") is False:
            continue
        price = parse_price(variant.get("price"))
        compare_at = parse_price(variant.get("compare_at_price"))
        # Only a genuine markdown counts - Shopify sometimes sets
        # compare_at_price equal to price, which isn't actually a sale.
        if price is None or compare_at is None or compare_at <= price:
            continue
        on_sale_variants.append((variant, price, compare_at))

    if not on_sale_variants:
        return []

    # Prefer showing the sizes that are actually yours, not every on-sale
    # variant (colour/size combos can otherwise list 5+ irrelevant sizes).
    # Falls back to the full list if none match, so a genuine find never
    # gets fully hidden just because we can't confirm it.
    matched_variants = [
        (v, p, c) for (v, p, c) in on_sale_variants
        if size_matches(v.get("title", ""), size_terms)
    ]
    my_size = bool(matched_variants)

    # Price the card from the cheapest variant YOU could actually buy -
    # otherwise a cheaper non-matching colour/size sets the price badge
    # and discount for an item that'd really cost you more.
    pricing_basis = matched_variants if my_size else on_sale_variants
    best_variant, price, compare_at = min(pricing_basis, key=lambda v: v[1])
    discount_pct = round((1 - price / compare_at) * 100)

    sizes = []
    for variant, v_price, _ in (matched_variants if my_size else on_sale_variants):
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
    if my_size:
        score += 20  # same size-match bonus as the Vinted scoring

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
        "estimated_total": price,  # retail price, no marketplace fee to add
        "photo": photo,
        "url": product_url,
        "seller": {"login": shop_name, "feedback_count": 0, "reputation": None},
        "discount_pct": discount_pct,
        "score": score,
        "my_size": my_size,
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


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    shop_watches = config.get("shop_watches", [])
    if not shop_watches:
        print("No shop_watches configured in config.json - nothing to do.")
        return

    global_exclude = config.get("global_exclude", [])
    my_sizes = config.get("my_sizes", {})
    size_terms = [term for terms in my_sizes.values() for term in terms]
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
                shop_cards.extend(build_cards(product, domain, name, global_exclude, price_history, size_terms))
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
