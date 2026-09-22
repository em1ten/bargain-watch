#!/usr/bin/env python3
"""Price-check an item you want to SELL against live Vinted comparables.

Bargain Watch's scanner answers "is this cheap enough to buy?". This
answers the mirror question - "what should I ask for mine?" - by pulling
what comparable items are actually listed at right now.

It deliberately reuses vinted_watch.py's session and search code rather
than reimplementing it. Vinted's internal API has broken three separate
ways in a month (endpoint retired, host moved, response fields packed
into an accessibility string); having one client means fixing that once.

IMPORTANT CAVEAT, and the reason the output says so out loud:
Vinted's search returns ACTIVE LISTINGS, not completed sales. Active
asks skew high, because the overpriced ones are exactly the items that
didn't sell and are still sitting there. Treat the median active ask as
a ceiling, not a target. The `suggested` figure below applies a haircut
for that; it is the number to actually list at.

Usage:
    python3 price_check.py "nudie jeans grim tim w32"
    python3 price_check.py "barbour bedale" --size "L" --catalog 5
    python3 price_check.py --batch items.json --out priced.json

Batch input is a JSON list of {"query": ..., "size": ..., "catalog": ...}
objects; everything but "query" is optional.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vinted_watch import (  # noqa: E402
    new_session,
    run_search,
    item_size,
    item_status,
    item_brand,
)

DEFAULT_DOMAIN = "www.vinted.co.uk"
DEFAULT_CURRENCY = "GBP"
DEFAULT_CATALOG = "5"  # menswear, same default as the buying scanner

# How far below the median active ask to suggest listing. Active asks are
# inflated by unsold stock (see module docstring); 0.85 puts the suggestion
# slightly under the middle of the pack, which is where things actually move
# without leaving money on the table.
ASK_HAIRCUT = 0.85

# Condition affects what a comparable is worth comparing to. An item listed
# as "New with tags" is not a comparable for a well-worn one. These are
# multipliers applied to the suggestion when the seller's own condition is
# known and differs from the typical condition of the comparables.
CONDITION_FACTOR = {
    "New with tags": 1.25,
    "New without tags": 1.10,
    "Very good": 1.00,
    "Good": 0.85,
    "Satisfactory": 0.65,
}


def _prices(items):
    """Seller asking prices only. `price` is the ask; `total_item_price`
    already includes the buyer protection fee, so using it would compare
    our ask against other people's fee-inclusive totals and push every
    suggestion too high."""
    out = []
    for it in items:
        obj = it.get("price") or {}
        amount = obj.get("amount")
        if amount is None:
            continue
        try:
            out.append(float(amount))
        except (TypeError, ValueError):
            continue
    return out


def _trim_outliers(prices, pct=0.10):
    """Drop the top and bottom 10%. One chancer asking £450 for a £60
    jacket shouldn't drag the median up, and neither should a mis-tagged
    accessory at £3."""
    if len(prices) < 5:
        return sorted(prices)
    s = sorted(prices)
    cut = max(1, int(len(s) * pct))
    return s[cut:-cut] or s


def price_check(session, query, catalog=DEFAULT_CATALOG, size=None,
                condition=None, per_page=40, domain=DEFAULT_DOMAIN):
    """Return a pricing summary for one item, from live comparables."""
    watch = {"search_text": query}
    if catalog:
        watch["catalog_ids"] = catalog

    items = run_search(
        session, domain, watch,
        currency=DEFAULT_CURRENCY,
        per_page=per_page,
        catalog_ids=catalog,
    )

    result = {
        "query": query,
        "size": size,
        "condition": condition,
        "n_raw": len(items),
        "checked": time.strftime("%Y-%m-%d"),
    }

    if not items:
        result.update({"n": 0, "note": "no comparables found - try a broader search"})
        return result

    # Narrowing to the same size makes a real difference on clothing: a
    # W30 and a W38 of the same jeans are not the same market. Only apply
    # it if it leaves enough to be meaningful, otherwise fall back to the
    # unfiltered set and say so.
    matched = items
    size_filtered = False
    if size:
        same_size = [
            it for it in items
            if size.lower().replace(" ", "") in (item_size(it) or "").lower().replace(" ", "")
        ]
        if len(same_size) >= 4:
            matched = same_size
            size_filtered = True

    prices = _prices(matched)
    if not prices:
        result.update({"n": 0, "note": "comparables found but none had a readable price"})
        return result

    trimmed = _trim_outliers(prices)
    median = statistics.median(trimmed)
    suggested = median * ASK_HAIRCUT

    if condition and condition in CONDITION_FACTOR:
        suggested *= CONDITION_FACTOR[condition]

    conditions = {}
    for it in matched:
        c = item_status(it) or "unknown"
        conditions[c] = conditions.get(c, 0) + 1

    result.update({
        "n": len(trimmed),
        "size_filtered": size_filtered,
        "low": round(min(trimmed), 2),
        "median": round(median, 2),
        "high": round(max(trimmed), 2),
        "suggested": round(suggested / 0.5) * 0.5,  # nearest 50p
        "condition_mix": dict(sorted(conditions.items(), key=lambda kv: -kv[1])),
        "sample_brands": sorted({item_brand(it) for it in matched[:10] if item_brand(it)}),
        "caveat": "active asking prices, not sold prices - the median is a ceiling",
    })
    return result


def format_result(r):
    if not r.get("n"):
        return f"  {r['query']}\n    {r.get('note', 'no data')}"
    size_note = " (same size only)" if r.get("size_filtered") else ""
    lines = [
        f"  {r['query']}",
        f"    {r['n']} comparables{size_note}   £{r['low']:.0f} — £{r['median']:.0f} — £{r['high']:.0f}   (low/median/high ask)",
        f"    suggest listing at £{r['suggested']:.2f}",
    ]
    if r.get("condition_mix"):
        mix = ", ".join(f"{k} ×{v}" for k, v in list(r["condition_mix"].items())[:3])
        lines.append(f"    comparable conditions: {mix}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="what the item is, as you'd search for it")
    ap.add_argument("--size", help="size to narrow comparables to (e.g. 'L', 'W32')")
    ap.add_argument("--condition", help="your item's condition, e.g. 'Very good'")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG, help="Vinted catalog id (default 5 = menswear)")
    ap.add_argument("--batch", help="JSON file of items to price in one go")
    ap.add_argument("--out", help="write results to this JSON file")
    args = ap.parse_args()

    if not args.query and not args.batch:
        ap.error("give a query or --batch")

    session = new_session(DEFAULT_DOMAIN)

    if args.batch:
        items = json.loads(Path(args.batch).read_text())
        results = []
        for i, spec in enumerate(items):
            if i:
                time.sleep(2)  # same pacing as the scanner - Vinted rate-limits hard
            r = price_check(
                session,
                spec["query"],
                catalog=spec.get("catalog", DEFAULT_CATALOG),
                size=spec.get("size"),
                condition=spec.get("condition"),
            )
            results.append(r)
            print(format_result(r))
    else:
        results = [price_check(
            session, args.query,
            catalog=args.catalog, size=args.size, condition=args.condition,
        )]
        print(format_result(results[0]))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {len(results)} result(s) to {args.out}")


if __name__ == "__main__":
    main()
