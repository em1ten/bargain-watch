#!/usr/bin/env python3
"""
Bargain Watch
-------------
Scans Vinted for a set of core searches, scores every listing, and writes one ranked feed
for the dashboard (docs/data.json). Notifications go out via ntfy.sh
for new finds on instant watches, and queue for the daily digest on
digest watches.

The dashboard is deliberately read-only: all configuration lives in
config.json in the repo (protected by your GitHub login), so the page
itself has no inputs, tokens, or write access to anything.

Scoring (roughly 0-100, higher = better find; weights live in
config.json "scoring" and default to the values below):
  + condition               (new with tags +45, new without tags +32,
                             very good +10, good 0)
  + price vs your own cap   (+30 anywhere from 15-50% of this watch's
                             price_to, tapering to 0 at the cap; under 15%
                             gets 0 - usually a different product or fake)
  + freshness               (first seen <6h +20, <24h +14, <3d +8, <7d +3,
                             still unsold after 14d -10)
  + price drop since last seen (+10)
  + explicitly your size    (+6; size-less items like bags get 0)
  - caution flags           (-20 each, caution-tier brands only)
"""

import json
import os
import random
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DOCS_DIR = ROOT / "docs"
DATA_PATH = DOCS_DIR / "data.json"
SEEN_PATH = ROOT / "seen_ids.json"

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

DEFAULT_SCORING = {
    "condition": {"New with tags": 45, "New without tags": 32, "Very good": 10, "Good": 0},
    "price_vs_cap_max": 30,
    # [max hours since first seen, points], checked in order
    "freshness": [[6, 20], [24, 14], [72, 8], [168, 3]],
    "stale_after_days": 14,
    "stale_penalty": 10,
    "price_drop": 10,
    "explicit_size": 6,
    "caution_flag_penalty": 20,
}
FRESH_HOURS = 24        # "Just listed" on the dashboard = first seen within this
HISTORY_KEEP_DAYS = 30  # forget items not seen for this long
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


# Notification state (today's push count, the digest queue, when the digest
# last went out). Kept inside seen_ids.json rather than a file of its own,
# because seen_ids.json is committed by every version of the workflow - a
# separate state file that didn't get committed is exactly why the old
# digest never worked.
NOTIFY_STATE = {}


def today_index(now=None):
    return int((now or time.time()) // 86400)


def load_price_history(path):
    """seen_ids.json, version 2: {"version": 2, "items": {id: [price,
    first_seen_unix_or_null, last_seen_day]}}.

    first_seen is what makes freshness scoring possible - Vinted's new
    search API doesn't return when an item was listed, so the first time
    this scan saw it is the best available proxy. last_seen is a day number
    (not a timestamp) on purpose: it then changes at most once a day per
    item, which keeps the committed file's git churn small, and it lets
    items that have disappeared be forgotten after HISTORY_KEEP_DAYS
    instead of the file growing forever (it had reached 180k ids / 4MB).

    Older formats are migrated: a flat list of ids, or {id: last_price}.
    Their first-seen time is unknown (null - scored as neither fresh nor
    stale) and they are given a last_seen 27 days back, so anything no
    longer turning up is pruned within about 3 days."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get("version") == 2:
        NOTIFY_STATE.clear()
        NOTIFY_STATE.update(data.get("notify") or {})
        return data.get("items", {})
    old_day = today_index() - 27
    if isinstance(data, list):
        return {str(i): [None, None, old_day] for i in data}
    migrated = {str(k): [v, None, old_day] for k, v in data.items()}
    print(f"Migrated {len(migrated)} items from the old seen_ids.json format")
    return migrated


def save_price_history(path, history):
    cutoff = today_index() - HISTORY_KEEP_DAYS
    kept = {k: v for k, v in history.items() if v[2] is not None and v[2] >= cutoff}
    pruned = len(history) - len(kept)
    lines = [json.dumps(k) + ":" + json.dumps(kept[k], separators=(",", ":")) for k in sorted(kept)]
    notify = json.dumps(NOTIFY_STATE, sort_keys=True, separators=(",", ":"))
    with open(path, "w") as f:
        f.write('{"version":2,"notify":' + notify + ',"items":{\n' + ",\n".join(lines) + "\n}}\n")
    if pruned:
        print(f"Forgot {pruned} item(s) not seen for {HISTORY_KEEP_DAYS}+ days")


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def new_session(domain, locale="en-GB"):
    """Vinted's site sets anti-bot cookies on first visit. Grab those
    before calling the API, same as a real browser would.

    The result of that first visit used to be ignored entirely. That
    matters because if it fails or gets blocked, every later API call goes
    out without valid cookies - and Vinted answers those with 404, not
    403, which is indistinguishable in the log from the API endpoint
    having been moved or retired. Reporting it makes the two causes
    tellable apart."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    session.headers["Locale"] = locale
    try:
        resp = session.get(f"https://{domain}/", timeout=15)
        cookie_names = sorted(session.cookies.keys())
        if resp.status_code != 200:
            print(f"! session setup: homepage returned {resp.status_code} (expected 200) - API calls may fail with 404")
        elif not cookie_names:
            print("! session setup: homepage returned 200 but set no cookies - API calls may fail with 404")
        else:
            print(f"Session established ({len(cookie_names)} cookies: {', '.join(cookie_names[:5])}{'...' if len(cookie_names) > 5 else ''})")

        # A real captured browser request (Sept 2026) showed X-Anon-Id and
        # X-CSRF-Token headers on every API call, not just cookies. Vinted's
        # current architecture may require these even for read-only search
        # requests. anon_id is already one of the cookies this session picks
        # up - it was just never read back out and sent as a header before.
        anon_id = session.cookies.get("anon_id")
        if anon_id:
            session.headers["X-Anon-Id"] = anon_id

        csrf_match = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
        if csrf_match:
            session.headers["X-CSRF-Token"] = csrf_match.group(1)
            print("  found CSRF token, added X-CSRF-Token header")
        else:
            print("  no CSRF token found in homepage HTML (may not be needed for GET requests, or the meta tag pattern has changed)")
    except requests.RequestException as e:
        print(f"! session setup failed: {e} - API calls will almost certainly fail")
    return session


def suggested_price_to(rrp):
    """When a watch has an rrp but no explicit price_to, derive a sensible
    ceiling instead of leaving the search unbounded or guessing blind.

    Fitted (log-log regression) against the 84 watches that already had
    both rrp and a manually-chosen price_to: price_to = 0.755 * rrp^0.873,
    R^2 = 0.92, mean absolute error ~£8. The pattern in that data is a
    real pricing instinct, not noise - the ratio of price_to/rrp drops
    smoothly from ~0.44 at low RRP down to ~0.28 at RRP=900, i.e. a
    smaller *percentage* ceiling for pricier brands. This only fires when
    price_to is missing entirely; any watch with an explicit price_to is
    never touched by this."""
    return round(0.755 * (rrp ** 0.873) / 5) * 5


MERGEABLE_LIST_KEYS = {"exclude", "exclude_ignore", "exclude_size"}


def resolve_watch(watch, subcategory_defaults=None):
    """Resolve a watch into its full effective config, without mutating the
    original dict.

    Two things happen here:

    1. Per-subcategory defaults are merged in underneath the watch's own
       values. This exists because a category like `tech` needs several
       protections applied consistently (authenticity checks, condition
       floor, damage-keyword excludes, catalog restriction) and repeating
       all of them per watch is how one silently goes missing. A watch's
       own explicit value always wins; list fields (`exclude`) merge rather
       than replace, so a per-item exclude adds to the category's baseline
       instead of quietly dropping it.

    2. A price_to is computed from rrp when one wasn't set explicitly.
    """
    resolved = dict(watch)

    defaults = (subcategory_defaults or {}).get(watch.get("subcategory"))
    if defaults:
        for key, default_value in defaults.items():
            if key not in resolved:
                resolved[key] = default_value
            elif key in MERGEABLE_LIST_KEYS and isinstance(default_value, list) and isinstance(resolved[key], list):
                # Merge only for exclude-style lists, where adding entries makes
                # the filter STRICTER. Never merge allow-lists like
                # allowed_conditions or brand_match: merging those silently
                # widens what gets through, which is the opposite of what
                # setting the field per-watch is meant to do.
                merged = list(resolved[key])
                merged.extend(v for v in default_value if v not in merged)
                resolved[key] = merged

    if not resolved.get("price_to") and resolved.get("rrp"):
        resolved["price_to"] = suggested_price_to(resolved["rrp"])
        print(f"  no price_to set for {watch['name']} - using computed cap £{resolved['price_to']} (from rrp £{resolved['rrp']})")

    return resolved


API_PATH_CANDIDATES = [
    # Confirmed working (Sept 2026, real captured browser request, 200 OK
    # with actual item results). Vinted moved the whole API off
    # www.vinted.co.uk onto this subdomain, with a different parameter
    # naming scheme too (attribute_ids[catalog] instead of catalog_ids) -
    # handled in _build_search_params below, not just a path change.
    "https://api.vinted.co.uk/svc-catalogue/items",
    # Old paths, kept only as a last-resort fallback - confirmed dead as of
    # Sept 2026 (404 on every request despite a healthy session), but cost
    # nothing to try if the new path ever stops working and the old one
    # somehow comes back.
    "/api/v2/catalog/items",
    "/api/v2/items",
]


def _build_search_params(watch, currency, per_page, catalog_ids, new_api, order=None):
    """Two parameter schemes, confirmed from real captured requests, not
    just a path change: the old www.vinted.co.uk API used catalog_ids=5
    and no page param; the new api.vinted.co.uk one uses page=1 and
    attribute_ids[catalog]=5 instead - a real bracket-notation query key,
    which requests percent-encodes correctly on its own.

    `order` matters far more on the new API than it did on the old one.
    The old API matched search_text strictly enough that newest_first gave
    genuinely relevant recent listings. The new API matches much more
    loosely - "sheffield wednesday retro vintage shirt" also matches items
    containing just "shirt" or "vintage" - so ordering those by newest
    returns whatever was listed most recently rather than what actually
    matches (observed: Uruguay and Peterborough shirts under a Sheffield
    Wednesday watch, with nearly every card flagged JUST LISTED). The real
    browser request uses relevance, so that's the default here.

    main() now asks for newest_first first and falls back to relevance
    when the newest page is mostly other brands; see the comment there.
    Freshness comes from seen_ids.json (first time this scan saw an item),
    never from the API's ordering."""
    ids = watch.get("catalog_ids", catalog_ids)
    effective_order = watch.get("order") or order or ("relevance" if new_api else "newest_first")
    if new_api:
        params = {
            "page": 1,
            "per_page": per_page,
            "search_text": watch["search_text"],
            "order": effective_order,
        }
        if ids:
            params["attribute_ids[catalog]"] = ids
    else:
        params = {
            "search_text": watch["search_text"],
            "order": effective_order,
            "per_page": per_page,
            "currency": currency,
        }
        if ids:
            params["catalog_ids"] = ids
    if watch.get("price_to"):
        params["price_to"] = watch["price_to"]
    if watch.get("price_from"):
        params["price_from"] = watch["price_from"]
    return params


def run_search(session, domain, watch, currency, per_page, catalog_ids, max_retries=3, api_paths=None, order=None):
    # Vinted moves its internal endpoints without warning - /api/v2/catalog/items
    # went from working to 404-on-every-request in September 2026 with the
    # session still perfectly healthy. Rather than hardcode one path, try each
    # candidate and report which one actually answers, so a change like that is
    # a config edit rather than a code change. A candidate starting with
    # "http" is used as a complete URL (different host); anything else is
    # treated as a path on the configured domain.
    paths = api_paths or API_PATH_CANDIDATES
    last_exc = None
    for path in paths:
        url = path if path.startswith("http") else f"https://{domain}{path}"
        params = _build_search_params(watch, currency, per_page, catalog_ids, new_api="api.vinted.co.uk" in url, order=order)
        for attempt in range(max_retries):
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                # Once Vinted starts rate-limiting, every subsequent request in
                # the same scan fails too if we just give up immediately - seen
                # live in scan #495, where one 429 cascaded into every remaining
                # watch failing and the feed going completely empty. Back off
                # and retry rather than let one rate-limit hit take out the
                # entire rest of the scan.
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)  # 5s, 10s, 15s
                    print(f"    rate-limited (429), waiting {wait}s before retry {attempt + 2}/{max_retries}")
                    time.sleep(wait)
                    continue
            if resp.status_code == 404:
                last_exc = requests.HTTPError(f"404 Client Error: Not Found for url: {resp.url}")
                break  # this path is gone - try the next candidate rather than retrying it
            try:
                resp.raise_for_status()
            except requests.RequestException as e:
                last_exc = e
                break
            return resp.json().get("items", [])
    if last_exc:
        raise last_exc
    return []


def passes_filters(item, exclude_terms, allowed_conditions=None, exclude_size_terms=None, require_title_any=None):
    title = (item.get("title") or "").lower()
    if any(term.lower() in title for term in exclude_terms):
        return False
    # Positive title requirement, for watches that can't use brand matching.
    # Vinted's new (Sept 2026) search matches far more loosely than the old
    # one: "sheffield wednesday retro vintage shirt" also matched Uruguay,
    # Peterborough and US college shirts on the generic words alone.
    # brand_match is no help for these - a football club isn't a registered
    # clothing brand, so sellers tag the manufacturer (Score Draw, Umbro)
    # instead. Requiring a distinctive token in the title is the only
    # defence left for that kind of watch.
    if require_title_any and not any(term.lower() in title for term in require_title_any):
        return False
    if exclude_size_terms:
        size = str(item_size(item)).lower()
        if any(term.lower() in size for term in exclude_size_terms):
            return False
    if allowed_conditions:
        status = str(item_status(item)).strip()
        if status and status not in allowed_conditions:
            return False
    return True


def _accessibility_label_fields(item):
    """Confirmed from a real scan (#514): the new API doesn't send brand,
    condition or size as separate fields at all. They're packed into one
    human-readable string meant for screen readers, on
    item_box.accessibility_label - e.g. "Nudie Jeans, Brand: Nudie Jeans,
    Condition: Very good, Size: W38, 50.00 £, 53.20 £". Parsed with named
    regex rather than positional splitting so it's tolerant of fields
    being reordered or omitted for other item types (electronics with no
    size, say). Cached on the item dict so three lookups (brand, size,
    status) on the same card don't reparse the same string three times."""
    cached = item.get("_parsed_label")
    if cached is not None:
        return cached
    label = ((item.get("item_box") or {}).get("accessibility_label")) or ""
    parsed = {}
    for key in ("Brand", "Condition", "Size"):
        match = re.search(rf"{re.escape(key)}:\s*([^,]+)", label)
        if match:
            parsed[key] = match.group(1).strip()
    item["_parsed_label"] = parsed
    return parsed


def item_field(item, *names, default=None):
    """Read the first present field from several candidate names.

    Vinted's September 2026 API migration didn't just move the endpoint and
    rename query parameters - the item response shape changed too, and the
    two failure modes were opposite and both bad:

      - brand_matches fails CLOSED on a missing brand field, so every
        brand-restricted watch (all the baby brands, the caution tier,
        Mitchell & Ness, New Era) silently returned zero results.
      - the size and condition filters fail OPEN on missing fields (a
        deliberate, correct choice originally, so genuinely size-less items
        like caps still pass) - so when the field vanished entirely, those
        filters silently stopped filtering anything at all.

    Rather than hardcode one name and break again on the next migration,
    try the known name first and then plausible alternatives. Also handles
    values that moved from a flat string to a nested object, which is a
    common shape change in API rewrites."""
    for name in names:
        value = item.get(name)
        if isinstance(value, dict):
            value = value.get("title") or value.get("name") or value.get("code")
        if value not in (None, "", [], {}):
            return value
    return default


def item_brand(item):
    direct = item_field(item, "brand_title", "brand", "brand_name", default="")
    if direct:
        return direct
    return _accessibility_label_fields(item).get("Brand", "")


def item_size(item):
    direct = item_field(item, "size_title", "size", "size_name", default="")
    if direct:
        return direct
    return _accessibility_label_fields(item).get("Size", "")


def item_status(item):
    direct = item_field(item, "status", "condition", "condition_title", "status_title", default="")
    if direct:
        return direct
    return _accessibility_label_fields(item).get("Condition", "")


def item_favourites(item):
    return item_field(item, "favourite_count", "favourites_count", "favourite_counts", default=0)


_SHAPE_REPORTED = False
_REPORTED_SHAPES = set()


def report_item_shape(items, watch_name=None, force_if_condition_missing=False):
    """Print the actual shape of an item, the first time this exact set of
    fields is seen in the scan - not just once globally.

    The original once-per-scan design only ever checked whichever watch
    happened to run first, which turned out to be a real blind spot: if a
    later watch's items have a different response shape (a real
    possibility - promoted/sponsored listings are flagged with
    content_source=search_promoted_items and may format differently),
    that would never get reported, because reporting had already switched
    itself off after the first watch. This reports again whenever an
    unseen field-name combination shows up, and also whenever condition
    specifically fails to resolve on a watch with a hard condition filter,
    since that's a real behavioural gap (used items get through), not
    just a cosmetic one (a missing badge)."""
    global _SHAPE_REPORTED
    if not items:
        return
    first = items[0]
    brand_resolved = bool(item_brand(first))
    size_resolved = bool(item_size(first))
    status_resolved = bool(item_status(first))
    condition_missing = not status_resolved
    # Two items can share identical top-level keys while having completely
    # different nested content (item_box present both times, but only one
    # has accessibility_label inside it) - so the key has to reflect
    # whether resolution actually succeeded, not just which keys exist.
    shape_key = (frozenset(first.keys()), brand_resolved, size_resolved, status_resolved)
    already_seen = shape_key in _REPORTED_SHAPES
    if already_seen:
        if force_if_condition_missing and condition_missing:
            print(f"  [shape] ({watch_name}) condition also unresolved here - same shape as already reported above")
        return
    _REPORTED_SHAPES.add(shape_key)
    _SHAPE_REPORTED = True
    label = f" ({watch_name})" if watch_name else ""
    print(f"  [shape]{label} item fields returned by the API: {', '.join(sorted(first.keys()))}")
    missing = []
    for lbl, resolved in (("brand", item_brand(first)), ("size", item_size(first)), ("status", item_status(first))):
        if resolved:
            print(f"  [shape] {lbl}: '{resolved}'")
        else:
            missing.append(lbl)
            print(f"  [shape] {lbl}: MISSING - filters depending on this are not working")
    if missing:
        # Dump a trimmed sample so the real field names are readable straight
        # from the log, rather than needing another devtools session.
        sample = {}
        for k, v in first.items():
            if isinstance(v, (dict, list)):
                sample[k] = json.dumps(v)[:120]
            else:
                sample[k] = v
        print(f"  [shape] could not resolve: {', '.join(missing)}. Full sample item below -")
        for k in sorted(sample):
            print(f"  [shape]   {k} = {sample[k]}")


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
    seller actually tagged the item as.

    When the brand field can't be resolved at all, fall back to matching
    against the title instead of rejecting outright. This matters because
    the September 2026 API migration renamed response fields: an
    unresolvable brand field made this return False for everything, so
    every brand-restricted watch silently returned zero results and the
    whole Baby section disappeared. A title match is weaker than Vinted's
    structured tag - it's the exact looseness this function exists to
    avoid - but returning nothing at all is worse, and the caller logs
    when the fallback is in use so it doesn't go unnoticed."""
    if not watch.get("require_brand_match", True):
        return True
    candidates = watch.get("brand_match") or [watch["name"]]
    actual = normalize_brand(item_brand(item))
    if not actual:
        title = normalize_brand(item.get("title") or "")
        if not title:
            return False
        for candidate in candidates:
            expected = normalize_brand(candidate)
            if expected and expected in title:
                return True
        return False
    for candidate in candidates:
        expected = normalize_brand(candidate)
        # Only "the tag contains the brand" ("Nudie Jeans Co" for Nudie
        # Jeans), never the reverse: the reverse let a "Drake" tag (rapper
        # merch) match Drake's, and would let a bare "Acne" tag (skincare)
        # match Acne Studios. Add alternative spellings to brand_match.
        if expected and expected in actual:
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

    favourites = item_favourites(item) or 0
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

    # BNWT at a steep discount is its own documented red flag for these
    # brands, separate from the hard-exclude floor above - fraud-prevention
    # guides specifically call out sellers with identical "new with tags"
    # designer stock as a counterfeit-sourcing pattern, not just implausibly
    # cheap pricing on its own. A listing can clear the 15% hard-exclude
    # floor comfortably (nowhere near "giving it away") and still fit this
    # profile - condition alone was previously a pure positive for these
    # brands with no downside, which is backwards for exactly this category.
    condition = str(item_status(item)).strip()
    bnwt_suspicious_ratio = watch.get("bnwt_suspicious_ratio", 0.4)
    if condition == "New with tags" and rrp and price_amount is not None and price_amount < rrp * bnwt_suspicious_ratio:
        caution_flags.append("Cheap for BNWT")

    return True, caution_flags


def size_matches(size_title, size_terms):
    """Match a size term as a distinct token within the listing's size string,
    not as a raw substring - otherwise 'L' wrongly matches inside 'XL', '38L',
    the '9' in 'UK 9' wrongly matches inside '39', or (critically) '9' wrongly
    matches inside '9.5' since a decimal point isn't alphanumeric and wasn't
    originally excluded as a boundary character. A trailing/leading '.'
    specifically means "this is part of a larger decimal number", so it's
    excluded from the boundary alongside letters and digits - a comma isn't,
    since '8.5, 9, 10' should still correctly match a standalone '9'.

    Returns the specific configured term that matched (e.g. 'W34'), not just
    True/False - the dashboard uses this to offer per-size filter pills
    (show only W34, not W36) rather than one blanket "my size" toggle."""
    if not size_terms:
        return None
    size = (size_title or "").strip().lower()
    if not size:
        return None
    for term in size_terms:
        t = term.strip().lower()
        if not t:
            continue
        pattern = r"(?<![a-z0-9.])" + re.escape(t) + r"(?![a-z0-9.])"
        if re.search(pattern, size):
            return term
    return None


def freshness_points(first_seen, now, scoring):
    """Points for how recently this scan first saw the item. Good BNWT
    pieces on Vinted tend to go within hours, so a find from this morning
    is worth far more than the same item still sitting there a fortnight
    later (which usually means it's overpriced)."""
    if first_seen is None:
        return 0  # migrated from the old history format: age unknown
    hours = max(0.0, (now - first_seen) / 3600)
    for max_hours, points in scoring["freshness"]:
        if hours <= max_hours:
            return points
    if hours > scoring["stale_after_days"] * 24:
        return -scoring["stale_penalty"]
    return 0


def score_card(condition, price_amount, price_to, first_seen, now, price_dropped, my_size, caution_flags, scoring):
    score = scoring["condition"].get(condition, 0)
    # Price against YOUR cap for this search, not a guessed brand RRP. The
    # RRP version gave nearly every listing 55-60 of its 60 possible points
    # (any cap is well under RRP), so 121 of 150 clothing cards scored
    # 80-99 and the ranking couldn't tell a BNWT find from a worn tee.
    # Full points anywhere from 15% to 50% of the cap, tapering to nothing
    # at the cap. Under 15% gets nothing: at that price it's almost always a
    # different, cheaper product (a keyring under a jacket brand, rapper
    # merch under Drake's) or a fake, not a real steal.
    if price_to and price_amount is not None and price_to > 0:
        ratio = price_amount / price_to
        if ratio < 0.15:
            fraction = 0.0
        elif ratio <= 0.5:
            fraction = 1.0
        else:
            fraction = max(0.0, (1 - ratio) / 0.5)
        score += scoring["price_vs_cap_max"] * fraction
    score += freshness_points(first_seen, now, scoring)
    if price_dropped:
        score += scoring["price_drop"]
    if my_size:
        score += scoring["explicit_size"]
    score -= scoring["caution_flag_penalty"] * len(caution_flags or [])
    return round(score)


def build_card(item, domain, watch, source, my_sizes, max_price, first_seen, now, previous_price=None, caution_flags=None, bargain_ceiling_ratio=0.6, scoring=None):
    scoring = scoring or DEFAULT_SCORING
    photo = (item.get("photo") or {}).get("url", "")
    # "price" is the seller's actual asking price. "total_item_price" is
    # already inclusive of Vinted's buyer protection fee - using it here
    # would double-count the fee once our own estimate is added below, and
    # would also skew every RRP/discount comparison against an inflated
    # number rather than the item's real asking price.
    price_obj = item.get("price") or item.get("total_item_price") or {}
    amount = price_obj.get("amount")
    currency = price_obj.get("currency_code", "")

    # Vinted's bulk search API only returns business/id/login/photo/
    # profile_url on the seller - NO feedback count or reputation
    # (confirmed via live diagnostic). Earlier versions stored those two
    # fields anyway, so they silently held 0/None on every single listing.
    # Only what's genuinely available is kept here; profile_url is useful
    # for checking a seller's real history manually in the browser.
    user = item.get("user") or {}
    seller = {
        "id": user.get("id"),
        "login": user.get("login", ""),
        "profile_url": user.get("profile_url", ""),
        "business": bool(user.get("business")),
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

    condition = str(item_status(item)).strip()
    size_title = str(item_size(item)).strip()
    size_terms = my_sizes.get(watch.get("size_category", ""), [])
    size_term_matched = size_matches(size_title, size_terms)
    my_size = size_term_matched is not None

    photo_obj = item.get("photo") or {}
    listed_at = (photo_obj.get("high_resolution") or {}).get("timestamp") or item.get("created_at_ts")
    try:
        listed_at = int(listed_at) if listed_at is not None else None
    except (TypeError, ValueError):
        listed_at = None
    # If Vinted ever sends a real listing time again, it beats "first seen".
    if listed_at and (first_seen is None or listed_at < first_seen):
        first_seen = listed_at

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

    score = score_card(
        condition, price_amount, watch.get("price_to"), first_seen, now,
        price_dropped, my_size, caution_flags, scoring,
    )
    is_new = first_seen is not None and (now - first_seen) <= FRESH_HOURS * 3600

    # "Bargain" used to mean "discount vs RRP >= bargain_threshold_pct". But every
    # watch's price_to ceiling was already set well below half of RRP -
    # that's the point of a sensible ceiling - so any item that passed the
    # search's own price cap had already cleared that threshold too.
    # Checked across the config: 84 of 85 watches guaranteed this. The
    # badge fired on effectively everything, making the Bargains pill
    # indistinguishable from All. A genuine bargain here means priced well
    # below what you already decided was your ceiling for this search -
    # not "cheap vs a shop price nobody's actually charging".
    price_to = watch.get("price_to")
    under_ceiling = (
        bargain_ceiling_ratio is not None
        and price_to
        and price_amount is not None
        and price_amount <= price_to * bargain_ceiling_ratio
    )
    is_bargain = under_ceiling or (
        max_price is not None and price_amount is not None and price_amount <= max_price
    )
    if under_ceiling:
        pct_under_ceiling = round((1 - price_amount / price_to) * 100)
        bargain_reason = f"-{pct_under_ceiling}% of cap"
        bargain_pct = pct_under_ceiling
    elif is_bargain:
        bargain_reason = f"Under {currency} {int(max_price)}"
        bargain_pct = round((1 - price_amount / max_price) * 100) if max_price and price_amount is not None else 0
    else:
        bargain_reason = None
        bargain_pct = None

    return {
        "id": item.get("id"),
        "title": item.get("title", "").strip(),
        "watch": watch["name"],
        "brand": str(item_brand(item)).strip(),
        "size": size_title,
        "size_term_matched": size_term_matched,
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
        "first_seen": first_seen,
        "is_bargain": is_bargain,
        "bargain_reason": bargain_reason,
        "bargain_pct": bargain_pct,
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
        "authenticity_caution": bool(watch.get("authenticity_caution")),
        "caution_flags": caution_flags or [],
        "favourite_count": item_favourites(item) or 0,
    }


DASHBOARD_URL = "https://em1ten.github.io/bargain-watch/"


NOTIFY_DEFAULTS = {
    "push_min_score": 90,        # roughly: BNWT, found in the last few hours, well under your cap
    "max_pushes_per_day": 5,     # after this, finds wait for the digest
    "quiet_hours": [22, 7],      # UK time; finds in this window wait for the digest
    "digest_hour": 8,            # first scan after this (UK time) sends the digest
    "digest_min_score": 70,
    "digest_per_section": {"Clothing": 10, "other": 3},
}
PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
SECTION_NAMES = {"baby": "Baby", "games": "Games & music", "music": "Games & music",
                 "football": "Football", "tech": "Tech", "caution": "Caution"}


def notify_settings(config):
    settings = json.loads(json.dumps(NOTIFY_DEFAULTS))
    for key, value in (config.get("notifications") or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value
    return settings


def notify_mode_for(watch):
    """push = can notify straight away; digest = only in the morning
    digest; off = never. Clothing defaults to push, everything else
    (baby, games, music, football, tech, caution) to digest - a £4 baby
    vest or a CD is worth knowing about, not worth a buzz. The old name
    "instant" still works and means push."""
    mode = watch.get("notify")
    if mode == "instant":
        return "push"
    if mode in ("push", "digest", "off"):
        return mode
    return "push" if watch.get("category", "clothing") != "electronics" else "digest"


def section_of(card):
    if card.get("category", "clothing") != "electronics":
        return "Clothing"
    return SECTION_NAMES.get(card.get("subcategory"), "Other")


def uk_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(time.time(), ZoneInfo("Europe/London"))
    except Exception:
        return datetime.utcfromtimestamp(time.time())


def in_quiet_hours(hour, quiet):
    if not quiet or len(quiet) != 2 or quiet[0] == quiet[1]:
        return False
    start, end = quiet
    return start <= hour < end if start < end else (hour >= start or hour < end)


def format_line(c):
    tags = []
    if c.get("price_dropped") and c.get("previous_price") is not None:
        tags.append(f"was £{c['previous_price']:.2f}")
    if c.get("condition_badge"):
        tags.append(c["condition_badge"])
    if c.get("size"):
        tags.append(c["size"])
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    price = f"£{c['price_amount']:.2f}" if c.get("price_amount") is not None else c.get("price", "")
    return f"{price} {c['title']}{tag_str} - {c['watch']}"


def notify_ntfy(topic, title, message, click=None, priority="default", tags=("shirt",), actions=None):
    """Publishes as JSON (handles £ and emoji cleanly, and allows buttons)."""
    if not topic:
        return False
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": PRIORITY.get(priority, 3),
        "tags": list(tags),
    }
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]
    try:
        r = requests.post("https://ntfy.sh/", json=payload, timeout=15)
        return getattr(r, "status_code", 200) < 400
    except requests.RequestException as e:
        print(f"  ! ntfy push failed: {e}")
        return False


def push_finds(topic, cards):
    """One notification for everything worth a buzz in this scan."""
    cards = sorted(cards, key=lambda c: c.get("score", 0), reverse=True)
    top = cards[0]
    if len(cards) == 1:
        title = f"Find: {top['title'][:60]}"
    else:
        title = f"{len(cards)} finds on Vinted"
    lines = [format_line(c) for c in cards[:6]]
    if len(cards) > 6:
        lines.append(f"…and {len(cards) - 6} more on the dashboard")
    actions = []
    for c in cards[:3]:
        price = f"£{c['price_amount']:.0f}" if c.get("price_amount") is not None else ""
        label = f"{price} {c['watch']}".strip().replace(",", " ").replace(";", " ")[:24]
        actions.append({"action": "view", "label": label, "url": c["url"]})
    click = top["url"] if len(cards) == 1 else DASHBOARD_URL
    return notify_ntfy(topic, title, "\n".join(lines), click=click, priority="high",
                       tags=("shirt", "sparkles"), actions=actions)


def compact(c):
    keep = ("id", "title", "watch", "price", "price_amount", "condition_badge", "size",
            "score", "url", "price_dropped", "previous_price", "category", "subcategory")
    return {k: c.get(k) for k in keep}


def queue_for_digest(cards):
    queue = NOTIFY_STATE.setdefault("queue", {})
    for c in cards:
        old = queue.get(str(c["id"]))
        if old is None or c.get("score", 0) >= old.get("score", 0):
            queue[str(c["id"])] = compact(c)
    if len(queue) > 150:  # keep the state small
        best = sorted(queue.values(), key=lambda c: c.get("score", 0), reverse=True)[:150]
        NOTIFY_STATE["queue"] = {str(c["id"]): c for c in best}


def maybe_send_digest(topic, settings):
    """The first scan after digest_hour (UK time) each day sends the best of
    what was queued, grouped by section, then clears the queue."""
    now_uk = uk_now()
    today = now_uk.date().isoformat()
    if now_uk.hour < settings["digest_hour"] or NOTIFY_STATE.get("digest_sent") == today:
        return
    queue = list((NOTIFY_STATE.get("queue") or {}).values())
    per_section = settings["digest_per_section"]
    sections = {}
    for c in sorted(queue, key=lambda c: c.get("score", 0), reverse=True):
        name = section_of(c)
        limit = per_section.get(name, per_section.get("other", 3))
        bucket = sections.setdefault(name, [])
        if len(bucket) < limit:
            bucket.append(c)
    picked = sum(len(v) for v in sections.values())
    if picked:
        order = ["Clothing"] + sorted(k for k in sections if k != "Clothing")
        parts = []
        for name in order:
            if sections.get(name):
                parts.append(name + "\n" + "\n".join(format_line(c) for c in sections[name]))
        print(f"Sending daily digest: {picked} of {len(queue)} queued item(s)")
        notify_ntfy(topic, f"Vinted digest: {picked} worth a look", "\n\n".join(parts),
                    click=DASHBOARD_URL, tags=("shirt", "calendar"))
    NOTIFY_STATE["queue"] = {}
    NOTIFY_STATE["digest_sent"] = today


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


def merged_scoring(config):
    scoring = json.loads(json.dumps(DEFAULT_SCORING))
    for key, value in (config.get("scoring") or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(scoring.get(key), dict):
            scoring[key].update(value)
        else:
            scoring[key] = value
    return scoring


def load_previous_feed():
    try:
        with open(DATA_PATH) as f:
            return json.load(f).get("feed", [])
    except (OSError, ValueError):
        return []


def main():
    config = load_config()
    domain = config.get("domain", "www.vinted.co.uk")
    currency = config.get("currency", "GBP")
    per_page = config.get("max_items_per_watch", 40)
    bargain_ceiling_ratio = config.get("bargain_ceiling_ratio", 0.6)
    max_price = config.get("bargain_max_price")
    my_sizes = config.get("my_sizes", {})
    feed_size = config.get("feed_size", 60)
    global_exclude = config.get("global_exclude", [])
    catalog_ids = config.get("catalog_ids", "5")  # 5 = Vinted's "Men" category
    api_paths = config.get("api_paths") or API_PATH_CANDIDATES
    search_order = config.get("search_order")  # set to force one order for every watch
    scoring = merged_scoring(config)
    notify_cfg = notify_settings(config)
    default_cap = config.get("max_in_feed_default", 8)
    fallback_min = config.get("relevance_fallback_min", 6)
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()

    now = int(time.time())
    today = today_index(now)
    price_history = load_price_history(SEEN_PATH)
    previous_feed = load_previous_feed()
    subcategory_defaults = config.get("subcategory_defaults", {})

    # Every watch, every scan. This used to alternate half of the digest
    # watches between scans to cut request volume - but the dashboard only
    # ever showed the latest scan, so at any moment about half of those
    # watches were simply missing from it (51 of 100 on 10 Nov).
    scan_plan = [resolve_watch(w, subcategory_defaults) for w in config["watches"]]

    session = new_session(domain, config.get("locale", "en-GB"))
    all_cards = []
    errors = []
    consecutive_404s = 0
    newest_failures = 0
    newest_disabled = False
    push_candidates = []
    digest_candidates = []
    aborted = False
    order_stats = Counter()

    for watch in scan_plan:
        name = watch["name"]
        # global_exclude was written with clothing/kids-junk in mind ('case'
        # for phone cases, 'replica' for fake designer items) but those same
        # words are completely ordinary in other categories - 'replica' is
        # the standard term for a genuine retro football shirt, and 'case'
        # shows up constantly in legitimate game/CD listings. exclude_ignore
        # lets a specific watch opt back in to a global term.
        ignore = set(watch.get("exclude_ignore", []))
        exclude_terms = [t for t in global_exclude if t not in ignore] + watch.get("exclude", [])
        notify_mode = notify_mode_for(watch)
        print(f"Checking: {name}")

        # Newest first, so each scan sees what was just listed rather than
        # the same 40 "most relevant" items every time. Vinted's text search
        # is loose, though ("oliver spencer" also matches plain "spencer"),
        # so the newest page can be mostly other brands that the brand check
        # then throws away. If fewer than `relevance_fallback_min` survive,
        # the relevance-ordered page is fetched as well and merged in.
        # Only brand-checked watches get the newest page: for a generic search
        # ("side adjuster trousers", vinyl, games) nothing would weed out the
        # loose matches, so those stay on relevance.
        forced = watch.get("order") or search_order
        if forced:
            orders = [forced]
        elif newest_disabled or not watch.get("require_brand_match", True):
            orders = ["relevance"]
        else:
            orders = ["newest_first", "relevance"]

        seen_here = set()
        accepted = 0
        fetched_ok = False
        last_error = None
        rejections = Counter()
        skipped_count = 0
        notify_cards = []
        raw_total = 0

        for idx, order in enumerate(orders):
            if idx > 0:
                if accepted >= fallback_min:
                    order_stats["newest page was enough"] += 1
                    break
                order_stats["needed the relevance page too"] += 1
                time.sleep(2)
            try:
                raw_items = run_search(session, domain, watch, currency, per_page, catalog_ids, api_paths=api_paths, order=order)
            except requests.RequestException as e:
                print(f"  ! request failed ({order}): {e}")
                last_error = e
                if order == "newest_first" and "404" not in str(e):
                    newest_failures += 1
                    if newest_failures >= 3:
                        newest_disabled = True
                        print("  ! newest_first failed 3 times in a row - using relevance only for the rest of this scan")
                continue
            fetched_ok = True
            if order == "newest_first":
                newest_failures = 0
            raw_total += len(raw_items)
            print(f"  {len(raw_items)} raw results ({order})")
            report_item_shape(raw_items, watch_name=name, force_if_condition_missing=bool(watch.get("allowed_conditions")))
            if len(raw_items) >= per_page and order == orders[-1]:
                print(f"  ! hit the {per_page}-result page cap - there may be more on Vinted than this scan saw")

            for item in raw_items:
                item_id_str = str(item.get("id"))
                if item_id_str in seen_here:
                    continue
                seen_here.add(item_id_str)
                try:
                    if not passes_filters(item, exclude_terms, watch.get("allowed_conditions"), watch.get("exclude_size"), watch.get("require_title_any")):
                        rejections["excluded keyword / wrong condition"] += 1
                        continue
                    if not brand_matches(item, watch):
                        rejections["brand tag mismatch"] += 1
                        continue
                    passes_auth, caution_flags = authenticity_caution_check(item, watch)
                    if not passes_auth:
                        rejections["caution hard-exclude"] += 1
                        continue
                    prev = price_history.get(item_id_str)
                    first_time = prev is None
                    first_seen = now if first_time else prev[1]
                    previous_price = None if first_time else prev[0]
                    card = build_card(
                        item, domain, watch, "core", my_sizes, max_price, first_seen, now,
                        previous_price, caution_flags, bargain_ceiling_ratio, scoring,
                    )
                except Exception as e:
                    skipped_count += 1
                    print(f"  ! skipped one malformed item: {e}")
                    continue

                price_history[item_id_str] = [card["price_amount"], card["first_seen"], today]

                # Hard size filter, not just a scoring nudge - this is a
                # personal wardrobe feed, so a listing in a size that isn't
                # wearable shouldn't appear at all. Only applies when the
                # watch has a size_category AND the listing states a size.
                if watch.get("size_category") and card["size"] and not card["my_size"]:
                    rejections["wrong size"] += 1
                    continue

                accepted += 1
                all_cards.append(card)
                if first_time or card["price_dropped"]:
                    notify_cards.append(card)

        if not fetched_ok:
            errors.append(name)
            consecutive_404s = consecutive_404s + 1 if last_error is not None and "404" in str(last_error) else 0
            if consecutive_404s >= 5:
                print(
                    "\n! ABORTING: 5 consecutive 404s on every candidate API path.\n"
                    f"  Tried: {', '.join(api_paths)}\n"
                    "  Vinted has probably moved or retired the endpoint. To find the current one:\n"
                    "    1. Open vinted.co.uk in a browser and search for anything\n"
                    "    2. F12 -> Network tab -> filter XHR\n"
                    "    3. Find the request that returns the listings JSON, copy its path\n"
                    "    4. Add it to config.json as: \"api_paths\": [\"/that/path\"]"
                )
                aborted = True
                break
            time.sleep(2)
            continue
        consecutive_404s = 0

        if skipped_count:
            print(f"  {skipped_count} item(s) skipped due to unexpected data")
        if rejections:
            breakdown = ", ".join(f"{reason}: {n}" for reason, n in rejections.most_common())
            print(f"  {accepted} kept; rejected - {breakdown}")
        if watch.get("require_brand_match", True) and raw_total and rejections.get("brand tag mismatch", 0) == raw_total:
            print(f"  ! every one of {raw_total} results failed brand matching - check [shape] above")

        # Sort this watch's new / price-dropped items into "worth a buzz now"
        # and "worth a line in the morning digest". Sending happens once,
        # after every watch has run.
        for c in notify_cards:
            if notify_mode == "push" and c["score"] >= notify_cfg["push_min_score"]:
                push_candidates.append(c)
            elif notify_mode != "off" and c["score"] >= notify_cfg["digest_min_score"]:
                digest_candidates.append(c)

        time.sleep(2)

    if order_stats:
        print("Search order: " + ", ".join(f"{k}: {v}" for k, v in order_stats.items()))
    # At most one push per scan, at most max_pushes_per_day a day, and none
    # in quiet hours. Anything held back goes into the digest instead.
    today_uk = uk_now().date().isoformat()
    if NOTIFY_STATE.get("push_day") != today_uk:
        NOTIFY_STATE["push_day"] = today_uk
        NOTIFY_STATE["pushes"] = 0
    if push_candidates:
        if not ntfy_topic:
            print(f"{len(push_candidates)} find(s) worth a push, but NTFY_TOPIC isn't set")
        elif in_quiet_hours(uk_now().hour, notify_cfg["quiet_hours"]):
            print(f"{len(push_candidates)} find(s) held for the digest (quiet hours)")
            digest_candidates.extend(push_candidates)
        elif NOTIFY_STATE.get("pushes", 0) >= notify_cfg["max_pushes_per_day"]:
            print(f"{len(push_candidates)} find(s) held for the digest (already sent {NOTIFY_STATE['pushes']} pushes today)")
            digest_candidates.extend(push_candidates)
        elif push_finds(ntfy_topic, push_candidates):
            NOTIFY_STATE["pushes"] = NOTIFY_STATE.get("pushes", 0) + 1
            print(f"Pushed {len(push_candidates)} find(s) (push {NOTIFY_STATE['pushes']} of {notify_cfg['max_pushes_per_day']} today)")
        else:
            digest_candidates.extend(push_candidates)
    queue_for_digest(digest_candidates)
    maybe_send_digest(ntfy_topic, notify_cfg)

    # A watch whose request failed this time keeps its cards from the last
    # scan, rather than vanishing from the dashboard until the next one.
    if errors and not aborted:
        failed = set(errors)
        carried = [c for c in previous_feed if c.get("watch") in failed and c.get("marketplace") != "shop"]
        if carried:
            print(f"Carrying forward {len(carried)} card(s) from the last scan for {len(failed)} failed watch(es)")
            all_cards.extend(carried)

    # De-dupe, rank, trim
    unique = {}
    for c in all_cards:
        existing = unique.get(c["id"])
        if existing is None or c["score"] > existing["score"]:
            unique[c["id"]] = c

    # Same-seller clustering: a seller with multiple caution-tier listings in
    # one scan is a real red flag (the three identical "Palm Angels" jumpers
    # that were really an unlabelled Moncler collab). No extra requests -
    # the seller id is already on every card.
    caution_by_seller = {}
    for c in unique.values():
        if c.get("authenticity_caution"):
            seller_key = c["seller"].get("id") or c["seller"].get("login")
            if seller_key:
                caution_by_seller.setdefault(seller_key, []).append(c)
    for seller_cards in caution_by_seller.values():
        if len(seller_cards) < 2:
            continue
        for c in seller_cards:
            c["score"] -= scoring["caution_flag_penalty"]
            c["caution_flags"].append(f"Seller has {len(seller_cards)} caution listings this scan")

    # Per-watch cap so one prolific brand can't fill the feed (Acne Studios
    # had 19 of the 150 clothing slots). max_in_feed on a watch overrides it.
    watch_caps = {w["name"]: w.get("max_in_feed", default_cap) for w in scan_plan}
    capped_cards = apply_watch_caps(list(unique.values()), watch_caps)

    def rank_key(c):
        return (-c["score"], -(c.get("first_seen") or 0))

    # Clothing and each electronics subcategory are ranked and trimmed
    # separately, so a busy subcategory can only crowd out more of itself.
    clothing_cards = [c for c in capped_cards if c.get("category", "clothing") != "electronics"]
    electronics_cards = [c for c in capped_cards if c.get("category") == "electronics"]

    # Reserve one slot per clothing watch first, then fill by score.
    clothing_by_watch = {}
    for c in clothing_cards:
        clothing_by_watch.setdefault(c["watch"], []).append(c)
    clothing_feed = []
    reserved_ids = set()
    for watch_cards in clothing_by_watch.values():
        watch_cards.sort(key=rank_key)
        clothing_feed.append(watch_cards[0])
        reserved_ids.add(watch_cards[0]["id"])
    remaining = sorted((c for c in clothing_cards if c["id"] not in reserved_ids), key=rank_key)
    clothing_feed.extend(remaining[: max(0, feed_size - len(clothing_feed))])
    clothing_feed.sort(key=rank_key)
    clothing_feed = clothing_feed[:feed_size]

    electronics_by_subcat = {}
    for c in electronics_cards:
        electronics_by_subcat.setdefault(c.get("subcategory") or "other", []).append(c)
    electronics_feed = []
    for subcat_cards in electronics_by_subcat.values():
        # Fewer red flags always ranks above more, whatever the price.
        subcat_cards.sort(key=lambda c: (len(c.get("caution_flags") or []),) + rank_key(c))
        electronics_feed.extend(subcat_cards[:feed_size])
    feed = clothing_feed + electronics_feed

    dashboard = {
        "generated_at": now,
        "errors": errors,
        "feed": feed,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    save_json(DATA_PATH, dashboard)
    save_price_history(SEEN_PATH, price_history)
    print(f"Done. {len(feed)} listings in the feed, {len(price_history)} items in history.")


if __name__ == "__main__":
    main()
