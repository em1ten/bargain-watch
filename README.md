# Bargain Watch

A minimal, self-hosted feed of the best current finds on Vinted for the
brands and styles you care about — ranked, deduplicated, and refreshed
automatically. Open it, best stuff's at the top, tap through. Nothing to
type, manage, or configure on the page itself.

Built to start with Vinted, with room to add other marketplaces later.

## How it works

- `vinted_watch.py` runs every 20 minutes (GitHub Actions), scans your core
  searches plus a daily rotation of discovery brands, scores every listing,
  and writes one ranked feed to `docs/data.json`
- `docs/index.html` (served via GitHub Pages) shows that feed with tap-only
  filter pills: All / My size / Just listed / Bargains / Starred / Games &
  Music / Football / Caution / Shops / Brands, plus a Sort row (Best match /
  Newest / Price) and, when the feed has sized items, a Size row underneath
  the main pills for narrowing to one exact size (e.g. just W34, not W36)
- Tapping **Brands** opens a panel of every brand currently in the feed —
  tapping one filters to just that brand. No text entry anywhere, so
  searching doesn't need a search box
- New finds push to your phone via ntfy.sh — instantly for priority brands,
  bundled into one daily digest for the rest (`digest_send.py`)

## The ranking

Every listing gets a score. Higher = better find, feed is sorted best-first:

| Signal | Points |
|---|---|
| Discount vs typical retail price (`rrp`) | up to 60 |
| At/under the flat bargain price (default £20) | +15 |
| Condition (new with tags → good) | +30 → +3 |
| Matches your size | +20 |
| New since the last scan | +5 |
| Price drop since last scan | +15 |
| Caution-tier brand, no flags raised | +10 |
| Caution-tier brand, per flag raised | −20 each |

There's no seller-reputation bonus — Vinted's bulk search API doesn't return
feedback count or reputation on the seller object, only
`business/id/login/photo/profile_url`, so it genuinely can't be scored from
data available in a single request. An earlier version of this scoring had a
dead +5 bonus checking fields that were never actually present; removed
rather than left silently doing nothing.

The caution penalty is the more important row: a flagged listing (new
seller, or suspiciously high unsold interest) doesn't just miss the clean
bonus, it's actively marked down enough that discount and condition alone
can't carry it to the top. Before this fix, an item with a steep "discount"
and "New with tags" condition — exactly the profile of a fake — could still
outrank everything else even while flagged, because the flag only withheld
a bonus rather than costing anything.

Separately: every electronics subcategory (Games & Music, Football,
Caution) is now trimmed to its own `feed_size` budget rather than one
shared pool. They never compete with each other on the dashboard — each
tab only ever shows its own subcategory — but the feed used to rank and
trim all of electronics together, so Caution's 11 brand searches could
silently crowd Games and Football (2 searches each) out of the feed
entirely, even when the scan found genuine matches for them.

## Scan #495: rate-limited into an empty feed

Real incident, not a hypothetical. The watch list roughly tripled this
session (~50 to 135 entries), and the 1-second pacing between requests
that was fine at the smaller scale started triggering Vinted's
rate-limiting. Once one request got a 429, the old code gave up on that
watch immediately and moved on - but Vinted kept rate-limiting every
subsequent request too, cascading through the rest of the scan
(including instant-priority watches) and ending in "Done. 0 listings in
the feed." Confirmed from the actual log: dozens of consecutive 429s,
total runtime only 2m10s (fast rejections, not slow timeouts - this was
a request-frequency problem, not a duration one).

Two fixes: `run_search` now retries a 429 up to twice more with backoff
(5s, then 10s) before giving up on that watch, rather than treating a
single rate-limit hit as fatal for the rest of the scan - tested against
both a recovering case (429 then success) and a persistent one (still
fails cleanly after exhausting retries, doesn't hang). The base delay
between requests also went from 1s to 2s, since the list has genuinely
outgrown what 1s was tuned for.

## Authenticity checks now work for any category, not just designer clothing

The whole caution-tier system (hard price floor vs RRP, seller-newness
flag, same-scan cross-listing fraud detection, clean-ranks-above-flagged
sorting) was hardcoded to `subcategory == "caution"` in three separate
places. Adding electronics meant generalizing this properly rather than
duplicating a weaker version of it for a new category - electronics
resale carries real fraud risk (non-working units sold as new, stolen
goods, non-delivery scams), arguably more than designer clothing, so it
deserved the strongest checks already built, not a lesser copy.

Every card now carries an explicit `authenticity_caution` flag (not just
the resulting flag list), and all three hardcoded checks were rewritten
to key off that instead of the literal subcategory name. This is a no-op
for every existing subcategory with no authenticity_caution cards (flag
count is always 0, so sorting falls straight through to score exactly as
before) - tested directly, confirmed zero behaviour change for Caution or
Football. But it means any future `"authenticity_caution": true` watch,
in any category, automatically gets the full protection - no more
special-casing needed per category.

## Tech — built to be extended, not hand-written each time

Own pill, own subcategory (`tech`), searching Vinted's Electronics
category (**2994** — the parent, so it covers video games, cameras,
audio, computers, wearables and the rest without needing each child ID).

The requirement was "real, working, clean and well looked after, and a
bargain" across whatever might come up in future — so rather than
hand-writing those protections per item and risking one silently going
missing, they live in `subcategory_defaults.tech` and are inherited
automatically. Adding anything new is a one-liner:

```json
{ "name": "Sony WH-1000XM4", "search_text": "sony wh-1000xm4", "rrp": 250, "subcategory": "tech" }
```

That alone resolves to: Electronics catalog restriction, full
authenticity checks (`authenticity_caution`), `min_favourites: 25`,
`max_in_feed: 3`, a condition floor of New/New-without-tags/Very good
(excludes "Good" and "Satisfactory" — the worn tiers), 22 damage and
scam keyword excludes, and a `price_to` computed from `rrp` via the
fitted formula. Mapping to the four requirements:

| Requirement | Mechanism |
|---|---|
| Real | `authenticity_caution` — hard price floor vs RRP, new-seller flag, same-scan cross-listing detection |
| Working | damage/scam keyword excludes (`faulty`, `spares or repair`, `untested`, `no charger`, `icloud locked`, …) |
| Clean, well looked after | condition floor — "Good" and "Satisfactory" never make the feed |
| Bargain | `price_to` + the global `bargain_ceiling_ratio` |

Per-watch overrides still work, with one deliberate asymmetry worth
knowing: `exclude` lists **merge** with the defaults (adding an exclude
makes a watch stricter, never weaker), while allow-lists like
`allowed_conditions` **replace** outright. That distinction was a real
bug when this was built — merging `allowed_conditions` silently widened
the filter back open, the exact opposite of what setting it per-watch is
meant to do. Meta Quest 2 uses that override to demand unused only.

**Honest limitation:** the bulk search API returns titles, never
descriptions. A seller who writes "screen has a scratch" or "controller
drifts" only in the description is invisible to every keyword check
here. Condition and price filters still apply, but for electronics
specifically, read the full listing before buying — these checks narrow
the field, they don't verify the item.

## Catching multi-listing sellers, without extra requests

Vinted's bulk search API genuinely doesn't return seller feedback or
review count at all - only `business, id, login, photo, profile_url`.
There was a real option to fetch full seller detail per item to close
this gap, but it was scoped back to just caution-tier survivors rather
than everything, since a blanket per-item request was already ruled out
early in this project as "40x the request volume - not worth it."

A cheaper alternative needed no extra requests at all: cross-reference
seller id across every caution-tier listing found in a single scan. This
is exactly the pattern behind the counterfeit-sourcing case that started
this thread - three identical "Palm Angels" jumpers (actually an
unlabelled Moncler collab), same seller, same size, sold as "1 for 25,
all 3 for 67." That listing passed completely clean because nothing
cross-referenced it against the seller's other listings in the same scan.

Any seller with 2+ caution-tier listings in one scan now gets a new flag,
"Seller has N caution listings this scan," with the same -20 penalty (and
loses the clean bonus if it had one) as any other caution flag. Tested
against the exact real case: all three identical listings drop from 125
to 95, while a genuine single-listing seller is untouched.

## Decimal size false positive, fixed for real this time

The original decimal false-positive risk ("9 matches inside 9.5, 43 inside
43.5") was already flagged as a known lesson in early project notes - but
the actual regex fix apparently never landed. Confirmed live: a "UK 9.5"
Clarks listing was matching size term "9" and showing the ✓ badge, purely
because the boundary regex only excluded letters and digits, not the
decimal point itself.

Fixed by excluding "." from the token boundary too - "9" no longer matches
inside "9.5". Deliberately did *not* exclude "," - a comma-separated size
list like "8.5, 9, 10" still needs a standalone "9" to match correctly,
and a comma there is a list separator, not part of a number.

## Every clothing watch gets a guaranteed slot

63 clothing watches now share one feed, with huge result-volume
differences between them (a common brand like Uniqlo vs a rare find like
Kiton or Auralee). Simply ranking and trimming that shared pool to
`feed_size` had the same crowding problem the electronics subcategories
had - tested against a plausible scenario (one high-volume watch, 62
quiet ones) and the old logic showed exactly **1** of 63 watches in the
feed. Every quiet watch was invisible even with genuine matches.

Fixed the same way as electronics: reserve one slot per watch first, then
fill whatever's left with the best remaining cards regardless of watch.
`feed_size` was raised from 60 to 150 to leave real room for that "best of
the rest" fill on top of the guaranteed reservations - with 63+ watches,
60 slots left no headroom once everyone's minimum was accounted for.

## Being clean matters more than being cheap, for caution-tier brands

Even with the "Cheap for BNWT" flag above, a flagged listing could still
theoretically outrank a clean one if its discount was big enough - the
caution penalty swings 30 points (−20 flagged vs +10 clean), but the
discount score alone can swing 60. Price could still occasionally beat
legitimacy under plain score sorting.

Caution-tier items are now sorted by flag count first, score only as a
tiebreaker - a listing with fewer red flags always ranks above one with
more, full stop, regardless of how good its price or condition looks.
This is enforced on both sides: the backend orders the feed this way, and
the dashboard's default "Best match" sort mirrors it, so switching sort
modes can't silently undo it by re-sorting on score alone.

## BNWT is not automatically reassuring for caution-tier brands

A £67 "New with tags" Palm Angels listing (actually an unlabelled Palm
Angels x Moncler collab, sold as three identical units, "1 for 25, all 3
for 67") ranked #1 in Caution with zero flags. It cleared the 15%
hard-exclude floor comfortably (67 vs a £45 floor on a £300 RRP), and
"New with tags" was previously a pure positive for every brand alike -
no downside, regardless of price.

Checked rather than assumed: fraud-prevention guides on Vinted fakes
specifically call out "multiple identical new-with-tags designer items
from one seller" as a counterfeit-sourcing pattern, and note fake BNWT
listings often reuse catalogue-style photos rather than genuine ones.
BNWT isn't reassuring for these brands - if anything it's the condition
most associated with freshly-produced fakes, since a counterfeiter is
never selling something worn.

`authenticity_caution_check` now flags "Cheap for BNWT" - condition is
"New with tags" and price is under `bnwt_suspicious_ratio` (default 0.4,
i.e. 40%) of RRP but still above the 15% hard-exclude floor. That middle
band used to pass completely clean; now it costs the same -20 caution
penalty as any other flag. A genuinely reasonable BNWT price (checked:
50% of RRP) still passes flag-free.

## What "Bargain" actually means now

The Bargains pill used to mean "discount vs `rrp` ≥ 50%". The problem:
every watch's own `price_to` ceiling was already set well below half of
`rrp` — that's the point of setting a sensible ceiling — so *any* item
that passed the search's own price cap had already cleared that
threshold too. Checked across the config: 84 of the 85 watches guaranteed
this. The badge fired on effectively everything, which is why Bargains
looked identical to All.

A genuine bargain now means priced at or under `bargain_ceiling_ratio`
(default 0.6, i.e. 60%) of that watch's own `price_to` — meaningfully
cheaper than what you already decided was your ceiling for that search,
not "cheap vs a shop price nobody's actually charging". The on-card badge
changed from `-77%` (vs a guessed RRP) to `-58% of cap` (vs your own
ceiling) to reflect what it's actually measuring.

## Discovery

`discovery_pool` in `config.json` holds adjacent brands worth knowing
(Arpenteur, A.P.C., Our Legacy, Stan Ray, Margaret Howell, and more). Each
day, 5 rotate in automatically and get scanned alongside your core watches —
their finds show up tagged "discovery" in the feed, so new brands surface
without you doing anything. The rotation cycles through the whole pool over
time.

## Deliberately read-only dashboard

The page has no text inputs, no tokens, and no write access to anything —
by design. All configuration lives in `config.json` in this repo, protected
by your GitHub login. To change anything (brands, sizes, price ceilings,
discovery pool, notification tiers), edit `config.json` on GitHub and
commit; the next scan picks it up. The only thing stored in the browser is
which listings you've starred, and that never leaves the browser.

## Setup (10 minutes)

1. **Create a GitHub repo** and push these files to it (`main` branch).

2. **Turn on GitHub Pages**
   Settings → Pages → Deploy from a branch → `main`, folder `/docs` → Save.
   Dashboard appears at `https://<username>.github.io/<repo>/`.

3. **Allow the Action to push updates**
   Settings → Actions → General → Workflow permissions →
   "Read and write permissions" → Save.

4. **(Optional) Phone notifications via ntfy.sh**
   Pick an unguessable topic name (e.g. `bw-yourname-8k2j` — ntfy topics are
   public to anyone who knows the name). Install the ntfy app and subscribe
   to it. Add it as a repo secret named `NTFY_TOPIC`
   (Settings → Secrets and variables → Actions).

5. **Run it once manually**
   Actions tab → "Vinted scan" → Run workflow. Refresh the dashboard after.

Then it runs itself.

## Adding a new watch without picking a price_to by hand

Every existing watch's `price_to` was set manually, brand by brand. Looking
at all 84 of those decisions together shows a real pattern, not noise: the
ratio of `price_to` to `rrp` drops smoothly from about 0.44 on cheap brands
down to about 0.28 on £900 ones (a smaller *percentage* ceiling for pricier
things - a sensible instinct, just never written down as a rule).

That pattern is now fitted as `price_to = 0.755 * rrp^0.873` (R² = 0.92,
mean error ~£8 against your own historical choices). Any new watch added
with just an `rrp` and no `price_to` gets one computed from this formula
automatically at scan time - the log shows `no price_to set for X - using
computed cap £Y`. Nothing with an explicit `price_to` is ever touched by
this; it only fills genuine gaps. If a computed cap looks wrong once you
see real results against it, just add an explicit `price_to` for that
watch and it takes over completely.

## config.json reference

Top-level:
- `my_sizes` — your sizes per category (`denim` / `tops` / `footwear`);
  powers the size flag and score bonus
- `bargain_max_price` — flat "always a bargain" price cutoff
- `bargain_ceiling_ratio` — how far under a watch's own `price_to` a listing
  must be to count as a "Bargain" (default 0.6, i.e. bottom 60% of the price
  range you already said you'd consider)
- `discovery_per_day` — how many discovery brands rotate in daily
- `feed_size` — how many listings the ranked feed keeps
- `global_exclude` — junk keywords filtered out of every search

Per watch (in `watches` or `discovery_pool`):
- `name`, `search_text` — label and what's searched on Vinted
- `price_to` — price ceiling for the search
- `rrp` — rough typical retail price (estimates — tune them), used for
  discount scoring
- `size_category` — which of `my_sizes` applies (`denim`/`tops`/`footwear`)
- `exclude` — extra junk keywords for just this watch
- `notify` (core watches only) — `instant`, `digest`, or `off`

## Notes and limits

- Uses Vinted's public search endpoint (the same one their website calls),
  not an official API — it could change without warning, needing a small
  script fix. Normal for tools like this.
- Every 20 minutes rather than truly real-time, to stay in GitHub's free
  tier and avoid hammering Vinted.
- Domain set to `www.vinted.co.uk` — change in `config.json` for other
  countries.
- For personal use finding items, not reselling automation.

## eBay (Technology / Games / Music)

`ebay_watch.py` is a separate, optional add-on — a different marketplace,
different API, different auth, kept fully decoupled from the Vinted scan
so a problem here can never break that. It writes its own file,
`docs/ebay_data.json`, which the dashboard fetches and merges in
alongside the main feed; if that file is missing or the fetch fails, the
Vinted feed just carries on as normal.

**Setup:**
1. Create an eBay Developer account at developer.ebay.com
2. Create a **Production** keyset (Your Account → Application Keys) —
   you'll get an **App ID (Client ID)** and **Cert ID (Client Secret)**
3. Add both as repo secrets: Settings → Secrets and variables → Actions →
   `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`
4. Run "Vinted scan" once manually — the eBay step runs alongside it

If the secrets aren't set, `ebay_watch.py` exits quietly and the rest of
the workflow (including the Vinted scan) runs completely unaffected.

**Honest limitation:** eBay deprecated third-party access to sold-item
price data in 2025 — the API used here (Browse API) only sees *active*
listings, the same situation as Vinted. So this doesn't solve the
RRP-estimate problem the way real sold-price data would have; `rrp`
values for eBay watches are still rough estimates, same as everywhere
else in this project.

eBay cards are tagged "· eBay" next to the brand label so you can always
tell which marketplace a listing came from, and use eBay's own
`itemWebUrl` link straight through to the real listing.

## Retail shops (Shopify) — genuine markdowns, no RRP guessing

`shop_watch.py` is a third, similarly decoupled add-on — checks specific
retailers directly for real sales, using Shopify's own `compare_at_price`
field rather than an estimated RRP. This is the most reliable price
signal in the whole project: Shopify tells you the actual "was" price
straight from the retailer, no guessing needed.

**The catch:** only works for shops that happen to run on Shopify. Not
every retailer does. Currently configured for Universal Works and Yards
Store (both confirmed Shopify) — add more to `shop_watches` in
`config.json` if you find other shops you like that are also on Shopify
(`{domain}/products.json` returning real product data is the quick way to
check).

No secrets or setup needed for this one — Shopify's product feed is
public. It'll just start working once `shop_watch.py` is uploaded and the
scan runs. Cards are tagged "· retail" and appear under the "Shops" pill,
alongside Technology/Games/Music/Football.

## Buyer protection fee estimate

Vinted charges buyers roughly 5% + £0.70 on top of the listed price at
checkout, so every Vinted card shows a compact "£X total w/ fee" line next
to the price. Shop (retail) cards don't show this — there's no marketplace
fee on a direct purchase from a retailer, so `estimated_total` is left
unset for those rather than repeating the same number.

## Diagnosing "missing" items

Each watch's Action log now ends with a rejection tally — e.g.
`rejected — wrong size: 12, brand tag mismatch: 3` — covering keyword
exclusions, condition filtering, brand mismatches, caution hard-excludes,
and wrong size, so it's possible to tell *why* something you saw browsing
Vinted directly didn't make the feed instead of guessing. If a watch's raw
result count hits `max_items_per_watch` (40 by default), the log also
flags that the scan may have been cut off before it saw everything Vinted
had — raise `max_items_per_watch` in `config.json` if that shows up often
on a watch.

## Price drops

`seen_ids.json` now tracks each listing's last-known price, not just
whether it's been seen. If a seller drops the price on something already
in the feed, it gets a red "↓ was £X" badge, a scoring bonus, and is
treated as notify-worthy the same as a brand-new listing — including
through the exceptional-find alert if the new price pushes its score high
enough. Trivial rounding changes (under 1% or 50p) don't count as a drop.

## Notifications

Actually need `NTFY_TOPIC` set as a repo secret to receive anything — see
setup step 4 above. Push messages now include the score, condition badge,
your-size flag, and (for price drops) the old price and % drop, not just
brand/price/title. The exceptional-find alert no longer double-fires for
`instant`-tier watches — it only covers `digest`/`off` watches now, since
`instant` watches already get pushed immediately through their normal
per-brand notification.

## Hard condition filter (not just scoring)

Most watches only use condition as a *scoring* signal — worn items can still
appear, just ranked lower. Some watches (currently the music ones) set
`"allowed_conditions"` instead, which is a hard filter: anything outside
that list never makes it into the feed at all, not just deprioritized. Add
this to any watch where "good condition only" needs to be a real rule, not
a preference.

## Games and music

Video games (PS5/Xbox), vinyl, and CDs are tracked the same way as
clothing — same scoring, shown together under the **Games & Music** pill
(previously two separate pills; combined since both were sparse enough on
their own that tabbing between two near-empty views wasn't worth it).
Vinyl and CDs use the hard condition filter above, since worn media isn't
worth the risk the way a worn jacket might still be fine.

Price ceilings were raised across the board here — £35 for games, £40 for
core vinyl, £30 for discovery vinyl, and £20 for CDs were likely cutting
out a lot of what Vinted actually had listed. Now £45 for games, £55 for
core vinyl, £45 for discovery vinyl, £30 for CDs. Worth checking the scan
log after a run or two: if raw result counts for these watches are still
low even at the new ceiling, the next thing to check is whether the
`catalog_ids` values (`2994,3002` for games; `3036,3041`/`3036,3039` for
vinyl/CDs) still match Vinted's current category structure — that's not
something checkable without seeing a live scan's actual result counts.

## Homeware and art

Original Art & Prints, Vintage Homeware, and Mid Century Decor round out
the "Other" pill — deliberately searched with terms like "original signed
print" and "mid century" rather than generic "wall art" or "decor", since
those broad terms mostly return mass-produced £2 Amazon posters on
Vinted, not the kind of well-made, different pieces this was meant to
surface.

## Discovery rotation and adding more brands

`discovery_per_day` controls how many discovery brands rotate in daily —
raised to 5 for more variety per visit. The discovery pool itself has grown
too; edit `discovery_pool` directly in `config.json` any time to add more.

Each discovery-tagged card also has a **"+ Add to my watches"** button. It
copies that brand's real search settings to your clipboard (no typing, no
GitHub token stored in the page) — paste the copied line into `watches` in
`config.json` on GitHub, then delete the matching entry from
`discovery_pool` so it's not scanned twice.

## Exceptional find alert

Any new listing that scores 90+ (tune via `exceptional_score_threshold`)
triggers an immediate high-priority push, regardless of that brand's own
`notify` setting — even a brand set to `digest` or `off` will alert
instantly for something this good. Runs as an extra check on top of your
normal per-brand notification settings, so an `instant`-tier brand's
exceptional find may briefly notify twice (once as its usual push, once as
the flagged "exceptional" one) — a minor duplicate in exchange for never
missing something genuinely excellent.

## "Other" (games, music, homeware, etc.) — tracked separately, not mixed into the main feed

Watches tagged `"category": "electronics"` in `config.json` (Film Cameras,
Hi-Fi & Turntables, Yeti) never appear in the default feed or any of the
other filters — they're only visible under the dedicated **"Other"**
pill. Scoring, discovery rotation, and notifications all work the same way
for them, they just don't get ranked alongside jackets and jeans. Any new
non-clothing watch just needs that same `"category": "electronics"` field
to stay out of the main feed.

## Sell List — the other direction (Sept 2026)

Bargain Watch answers "is this cheap enough to buy?". The Sell List answers
the mirror question, "what should I ask for mine?", for a wardrobe/tech
clear-out. It's a separate page on the same site (`docs/sell.html`), reading
`docs/sell.json`, with a link between the two in the header.

It is deliberately **not** a form to fill in. The bottleneck in any clear-out
is data entry, and a form doesn't fix that — you'd fill it in eleven times and
abandon the pile. Instead: photograph a batch, send the photos to Claude in
chat, and the identification, pricing, listing copy and platform call come
back written into `sell.json`. The page is then a phone-friendly view of that
while you're actually packing: copy the title, copy the description, tap the
status on.

### price_check.py

Prices the items against live Vinted comparables, reusing `vinted_watch.py`'s
session and search code rather than reimplementing it. Vinted's internal API
has broken three separate ways in a month (endpoint retired, host moved,
response fields packed into an accessibility string) — one client means
fixing that once.

```
python3 price_check.py "nudie jeans grim tim w32" --size W32 --condition "Very good"
python3 price_check.py --batch items.json --out priced.json
```

Two things it does that a raw search doesn't:

- **Trims the top and bottom 10%** of asks, so one chancer asking £450 for a
  £60 jacket doesn't drag the median up, and a mis-tagged accessory at £3
  doesn't drag it down.
- **Narrows to the same size** when there are at least four comparables in it
  — a W30 and a W38 of the same jeans are not the same market — and says so
  when it has, falling back to the unfiltered set when it can't.

The caveat it prints on every result matters and is not boilerplate: Vinted
search returns **active listings, not completed sales**. Active asks skew
high, because the overpriced ones are exactly the items that didn't sell and
are still sitting there. The median is a ceiling, not a target, so the
`suggested` figure applies a haircut (0.85) and then a condition multiplier.

### Status tracking

`to_list → listed → sold → posted`, cycled by tapping the status pill. Saved
in `localStorage`, per device — the page is static and can't write back to the
repo, same read-only principle as the main dashboard. The `status` field in
`sell.json` is only the starting point; a local tap overrides it. Telling
Claude "sold the Barbour" updates the JSON authoritatively if the two drift.

### Safety checklists

Each item carries a per-platform checklist, because the threats genuinely
differ by platform and by what you're selling:

- **Vinted** (clothing): the risks are off-platform contact, fake payment
  confirmations and address-change requests. Payment is only real when it's in
  your Vinted balance — never an email or a screenshot.
- **eBay** (electronics): the dominant threat is *return* fraud, not fake
  payment — the switcheroo (buyer returns their broken unit) and the weighted
  empty box. Serial/IMEI in the listing description is the single strongest
  defence, because it makes a swap provable.
- **Facebook** (bulky/local): cash on collection only; "my courier will
  collect and pay you" is always a scam.
- **CEX**: guaranteed sale, zero scam risk, noticeably less money.

Both Vinted and eBay are now effectively free for private sellers (Vinted
takes £0 from sellers; eBay UK dropped private-seller final value fees in
October 2024, with 300 free listings a month), so platform choice is about
what sells there and what the risk profile is — not about fees.

### Tax

Selling your own used possessions isn't trading, so it isn't taxable income
regardless of value. The 30-items / €2,000 threshold that triggers a scary
platform message is DAC7 *reporting*, not a tax bill. The £1,000 trading
allowance only starts to matter for things bought specifically to resell.
