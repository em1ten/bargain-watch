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
import random
import re
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

# A small set of realistic, current browser identities rather than one
# static string used on every single request forever - a real browser
# varies by device and gets version-bumped over time, so always sending
# the exact same fingerprint is itself a (mild) tell. Rotating between a
# few plausible ones per run is a reasonable improvement, though worth
# being honest that it can't disguise the underlying network origin -
# GitHub Actions runners share a known datacenter IP range regardless of
# what headers are sent, so this helps with fingerprinting, not IP-based
# blocking specifically.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Edg/124.0 Safari/537.36",
]

REQUEST_HEADERS = {
    "User-Agent": random.choice(USER_AGENTS),
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
CONDITION_BADGE_ELECTRONICS = {
    "New with tags": "NEW",
    "New without tags": "NEW",
}


def get_condition_badge(condition, category):
    """'BNWT'/'UNWORN' only make sense for wearable items - a game, record,
    or camera can't be 'unworn'. Same underlying Vinted condition options,
    different wording depending on what's actually being described."""
    if category == "electronics":
        return CONDITION_BADGE_ELECTRONICS.get(condition)
    return CONDITION_BADGE.get(condition)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_json_set(path):
    if path.exists():
        with open(path) as f:
            return set(json.load(f))
    return set()


def load_price_history(path):
    """seen_ids.json used to be a flat list of item IDs. Now it's a dict of
    {id: last_known_price} so price drops can be detected. Old-format files
    are migrated on first load - prices are unknown for those, so no drop
    gets (falsely) reported for anything already tracked pre-upgrade."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return {str(i): None for i in data}
    return data


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


def run_search(session, domain, watch, currency, per_page, catalog_ids):
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
    ids = watch.get("catalog_ids", catalog_ids)
    if ids:
        params["catalog_ids"] = ids

    url = f"https://{domain}/api/v2/catalog/items"
    resp = session.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("items", [])


def passes_filters(item, exclude_terms, allowed_conditions=None):
    title = (item.get("title") or "").lower()
    if any(term.lower() in title for term in exclude_terms):
        return False
    if allowed_conditions:
        status = (item.get("status") or "").strip()
        if status and status not in allowed_conditions:
            return False
    return True


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


def authenticity_caution_check(item, watch):
    """For watches opted into 'authenticity_caution' (currently Stone
    Island / CP Company / Belstaff / Canada Goose / Moncler, brands with a
    real counterfeit problem where no technical signal can actually detect
    a fake). This makes NO claim to spot counterfeits - it only excludes
    an implausibly low price on its own (a genuine seller essentially
    never gives away a real £350+ RRP item for a few pounds), plus the
    combination of a brand-new seller account and low genuine interest,
    and flags other things worth a second look before you scan the
    Certilogo tag or pay for Item Verification yourself.

    IMPORTANT: Vinted's bulk search results do NOT include seller feedback
    count or reputation - confirmed via diagnostic, the seller object only
    has business/id/login/photo/profile_url. Earlier versions of this
    function checked feedback/reputation fields that never actually
    existed in this data, so every listing silently failed that check
    regardless of the real seller's standing. Removed rather than left
    pretending to work - only signals that are genuinely present are used
    below.

    Returns (passes: bool, caution_flags: list[str])."""
    if not watch.get("authenticity_caution"):
        return True, []

    favourites = item.get("favourite_count") or 0
    is_new_seller = bool(item.get("show_1st_time_seller_discount"))

    min_favourites = watch.get("min_favourites", 15)
    high_favourites = watch.get("high_favourites_caution", 100)
    min_price_ratio = watch.get("min_authenticity_price_ratio", 0.15)

    # An implausibly low price relative to RRP is a hard exclude on its
    # own - same sanity-floor logic already used to suppress misleading
    # discount badges, but here it actually removes the listing rather
    # than just hiding the badge, since for these specific brands a price
    # this far below plausible is itself a strong red flag.
    rrp = watch.get("rrp")
    price_obj = item.get("price") or item.get("total_item_price") or {}
    price_amount = None
    try:
        price_amount = float(price_obj.get("amount")) if price_obj.get("amount") is not None else None
    except (TypeError, ValueError):
        pass
    if rrp and price_amount is not None and price_amount < rrp * min_price_ratio:
        return False, []

    low_interest = favourites < min_favourites

    # Hard exclude: brand new account AND barely anyone interested,
    # together. Everything else stays visible with a caution flag rather
    # than being silently removed.
    if is_new_seller and low_interest:
        return False, []

    caution_flags = []
    if is_new_seller:
        caution_flags.append("New seller")
    if favourites > high_favourites:
        caution_flags.append("High interest, unsold")

    return True, caution_flags


def size_matches(size_title, size_terms):
    """Match a size term as a distinct token within the listing's size string,
    not as a raw substring - otherwise 'L' wrongly matches inside 'XL', '38L',
    or the '9' in 'UK 9' wrongly matches inside '39'."""
    if not size_terms:
        return False
    size = (size_title or "").strip().lower()
    if not size:
        return False
    for term in size_terms:
        t = term.strip().lower()
        if not t:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])"
        if re.search(pattern, size):
            return True
    return False


def build_card(item, domain, watch, source, my_sizes, threshold_pct, max_price, is_new, previous_price=None, caution_flags=None):
    photo = (item.get("photo") or {}).get("url", "")
    # "price" is the seller's actual asking price. "total_item_price" is
    # already inclusive of Vinted's buyer protection fee - using it here
    # would double-count the fee once our own estimate is added below, and
    # would also skew every RRP/discount comparison against an inflated
    # number rather than the item's real asking price.
    price_obj = item.get("price") or item.get("total_item_price") or {}
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
        # If the price is implausibly low relative to the brand's RRP, it's
        # more likely a different (naturally cheaper) product from that brand
        # - a cap vs a jacket, an accessory vs a coat - than a genuine steep
        # discount. Skip the discount claim rather than show a misleading
        # -95%+ badge on something that was probably just never that pricey.
        if price_amount >= rrp * 0.15:
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

    # A genuine price drop since the last scan - not "new", but worth
    # surfacing the same way. Ignore trivial rounding noise (<1% or <£0.50).
    price_dropped = False
    price_drop_pct = None
    if previous_price is not None and price_amount is not None and previous_price > 0:
        drop = previous_price - price_amount
        if drop > 0.5 and drop / previous_price > 0.01:
            price_dropped = True
            price_drop_pct = round((drop / previous_price) * 100)

    # Rough estimate of what a buyer actually pays at checkout - Vinted's
    # Buyer Protection fee is ~5% of the item price plus a small fixed fee.
    # Shown so the price badge isn't misleading about the real total cost.
    estimated_total = None
    if price_amount is not None:
        estimated_total = round(price_amount * 1.05 + 0.7, 2)

    score = 0
    if discount_pct is not None and discount_pct > 0:
        score += min(discount_pct, 60)
    if max_price is not None and price_amount is not None and price_amount <= max_price:
        score += 15
    score += CONDITION_SCORES.get(condition, 0)
    # NOTE: no seller-reputation scoring bonus - Vinted's bulk search API
    # doesn't return feedback count or reputation on the seller object
    # (confirmed via diagnostic: only business/id/login/photo/profile_url
    # are present), so this can't be computed from data actually available
    # here without a separate per-item request. Previously there was a
    # dead +5 bonus that could never fire since it checked fields that
    # never existed in this response.
    if my_size:
        score += 20
    if is_new:
        score += 5
    if price_dropped:
        score += 15
    # For authenticity_caution watches (Designer pill) specifically: a
    # completely clean pass - no new-seller flag, no low/high favourite
    # concern - ranks above an otherwise-identical listing that got a
    # caution flag, so a quick scan of the top of the feed surfaces the
    # cleanest-looking finds first rather than treating flagged and
    # unflagged listings as equivalent.
    if watch.get("authenticity_caution") and not caution_flags:
        score += 10

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
        "price_amount": price_amount,
        "estimated_total": estimated_total,
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
        "condition_badge": get_condition_badge(condition, watch.get("category", "clothing")),
        "category": watch.get("category", "clothing"),
        "subcategory": watch.get("subcategory"),
        "price_dropped": price_dropped,
        "price_drop_pct": price_drop_pct,
        "previous_price": previous_price if price_dropped else None,
        "source_config": {
            k: watch[k]
            for k in ("search_text", "price_to", "price_from", "rrp", "size_category", "catalog_ids", "exclude")
            if k in watch
        },
        "caution_flags": caution_flags or [],
        "favourite_count": item.get("favourite_count") or 0,
    }


def format_line(c):
    tags = []
    if c.get("price_dropped"):
        tags.append(f"was {c['previous_price']:.2f}, -{c['price_drop_pct']}%")
    if c.get("condition_badge"):
        tags.append(c["condition_badge"])
    if c.get("my_size"):
        tags.append("your size")
    if not c.get("price_dropped") and c.get("bargain_reason"):
        tags.append(c["bargain_reason"])
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    return f"{c['watch']} — {c['price']}: {c['title']}{tag_str} (score {c['score']})"


def notify_ntfy(topic, title, cards, priority="default", tags="shirt"):
    if not topic or not cards:
        return
    lines = [format_line(c) for c in cards[:5]]
    message = "\n".join(lines)
    if len(cards) > 5:
        message += f"\n…and {len(cards) - 5} more"
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  ! ntfy push failed: {e}")


def apply_watch_caps(cards, watch_caps):
    """Cap how many listings from a single watch can appear in the final
    feed, regardless of how well they score - keeps one prolific brand
    (lots of listings, not necessarily lots of *quality*) from crowding
    out everything else. Keeps the highest-scoring items from that watch
    up to its cap; watches with no cap set are unaffected."""
    if not watch_caps:
        return cards
    counts = {}
    result = []
    for c in sorted(cards, key=lambda c: c["score"], reverse=True):
        cap = watch_caps.get(c["watch"])
        if cap is None:
            result.append(c)
            continue
        if counts.get(c["watch"], 0) < cap:
            result.append(c)
            counts[c["watch"]] = counts.get(c["watch"], 0) + 1
    return result


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
    catalog_ids = config.get("catalog_ids", "5")  # 5 = Vinted's "Men" category
    exceptional_threshold = config.get("exceptional_score_threshold", 90)
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    price_history = load_price_history(SEEN_PATH)
    digest_pending = {}
    if DIGEST_PATH.exists():
        with open(DIGEST_PATH) as f:
            digest_pending = json.load(f)

    discovery_today = pick_discovery(config.get("discovery_pool", []), config.get("discovery_per_day", 3))
    scan_plan = [(w, "core") for w in config["watches"]] + [(w, "discovery") for w in discovery_today]

    session = new_session(domain)
    all_cards = []
    errors = []
    exceptional_finds = []

    for watch, source in scan_plan:
        name = watch["name"]
        exclude_terms = global_exclude + watch.get("exclude", [])
        notify_mode = watch.get("notify", "instant")
        print(f"Checking ({source}): {name}")
        try:
            raw_items = run_search(session, domain, watch, currency, per_page, catalog_ids)
        except requests.RequestException as e:
            print(f"  ! request failed: {e}")
            errors.append(name)
            continue
        print(f"  {len(raw_items)} raw results from Vinted before any filtering")

        # ONE-TIME DIAGNOSTIC - checking whether seller feedback data is
        # actually present in bulk search results, or whether it's only
        # populated on the individual item page (same class of gap as the
        # verification-badge and description-text findings earlier).
        if raw_items and not globals().get("_dumped_user_keys"):
            globals()["_dumped_user_keys"] = True
            sample_user = raw_items[0].get("user") or {}
            print("  --- DIAGNOSTIC: full field list for one item's 'user' object ---")
            print(f"  {sorted(sample_user.keys())}")
            print(f"  Raw values: {sample_user}")
            print("  --- END DIAGNOSTIC ---")

        notify_cards = []  # new OR price-dropped - both worth alerting on
        skipped_count = 0
        for item in raw_items:
            try:
                if not passes_filters(item, exclude_terms, watch.get("allowed_conditions")):
                    continue
                if not brand_matches(item, watch):
                    continue
                passes_auth, caution_flags = authenticity_caution_check(item, watch)
                if not passes_auth:
                    continue
                item_id = item.get("id")
                item_id_str = str(item_id)
                is_new = item_id_str not in price_history
                previous_price = price_history.get(item_id_str)
                card = build_card(
                    item, domain, watch, source, my_sizes, threshold_pct, max_price, is_new, previous_price,
                    caution_flags,
                )
            except Exception as e:
                skipped_count += 1
                print(f"  ! skipped one malformed item: {e}")
                continue

            # Hard size filter, not just a scoring nudge - this is a personal
            # wardrobe feed, not a resale tool, so a listing in a size that
            # isn't wearable shouldn't appear at all. Only applies when the
            # watch has a size_category AND the listing actually states a
            # size - blank/one-size items (accessories, caps) still pass
            # through, since fit doesn't apply to them.
            if watch.get("size_category") and card["size"] and not card["my_size"]:
                price_history[item_id_str] = card["price_amount"]
                continue

            all_cards.append(card)
            price_history[item_id_str] = card["price_amount"]

            if is_new or card["price_dropped"]:
                notify_cards.append(card)
                # Exceptional-score alert is a cross-brand safety net for
                # anything that might get missed - skip it for "instant"
                # watches since they already get pushed immediately below,
                # so it doesn't fire twice for the same item.
                if notify_mode != "instant" and card["score"] >= exceptional_threshold:
                    exceptional_finds.append(card)
        if skipped_count:
            print(f"  {skipped_count} item(s) skipped due to unexpected data")

        if notify_cards and source == "core":
            new_count = sum(1 for c in notify_cards if c["is_new"])
            drop_count = sum(1 for c in notify_cards if c["price_dropped"])
            label_bits = []
            if new_count:
                label_bits.append(f"{new_count} new")
            if drop_count:
                label_bits.append(f"{drop_count} price drop{'s' if drop_count != 1 else ''}")
            print(f"  {', '.join(label_bits)}")
            if notify_mode == "instant":
                notify_ntfy(ntfy_topic, f"Vinted: {', '.join(label_bits)} — {name}", notify_cards)
            elif notify_mode == "digest":
                bucket = digest_pending.setdefault(name, [])
                existing = {c["id"] for c in bucket}
                bucket.extend(c for c in notify_cards if c["id"] not in existing)

        time.sleep(1)  # be polite between requests

    if exceptional_finds:
        print(f"{len(exceptional_finds)} exceptional find(s) (score >= {exceptional_threshold}) this scan")
        notify_ntfy(
            ntfy_topic,
            f"🔥 Exceptional find{'s' if len(exceptional_finds) > 1 else ''}",
            exceptional_finds,
            priority="urgent",
            tags="rotating_light",
        )

    # De-dupe (a discovery brand can overlap a core search), rank, trim
    unique = {}
    for c in all_cards:
        existing = unique.get(c["id"])
        if existing is None or c["score"] > existing["score"]:
            unique[c["id"]] = c

    all_watches = config["watches"] + config.get("discovery_pool", [])
    watch_caps = {w["name"]: w["max_in_feed"] for w in all_watches if "max_in_feed" in w}
    capped_cards = apply_watch_caps(list(unique.values()), watch_caps)

    # Clothing and electronics are ranked and trimmed separately - electronics
    # items structurally can't score as high (no size-match bonus, often no
    # RRP set), so ranking them together let clothing silently squeeze every
    # electronics result out of the top N, even when the scan found genuine
    # matches.
    clothing_cards = [c for c in capped_cards if c.get("category", "clothing") != "electronics"]
    electronics_cards = [c for c in capped_cards if c.get("category") == "electronics"]
    clothing_feed = sorted(clothing_cards, key=lambda c: c["score"], reverse=True)[:feed_size]
    electronics_feed = sorted(electronics_cards, key=lambda c: c["score"], reverse=True)[:feed_size]
    feed = clothing_feed + electronics_feed

    dashboard = {
        "generated_at": int(time.time()),
        "discovery_today": [w["name"] for w in discovery_today],
        "errors": errors,
        "feed": feed,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_json(SEEN_PATH, price_history)
    save_json(DIGEST_PATH, digest_pending)
    print(f"Done. {len(feed)} listings in the feed.")


if __name__ == "__main__":
    main()
