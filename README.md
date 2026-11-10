# Bargain Watch

A minimal, self-hosted feed of the best current finds on Vinted for the
brands and styles you care about — ranked, deduplicated, and refreshed
automatically. Open it, best stuff's at the top, tap through. Nothing to
type, manage, or configure on the page itself.

Built to start with Vinted, with room to add other marketplaces later.

## How it works

- `vinted_watch.py` runs from GitHub Actions (aim: every 20 minutes - see
  **Reliable scan timing** below, because GitHub's own scheduler doesn't
  manage that), runs every watch in `config.json`, scores every listing and
  writes one ranked feed to `docs/data.json`
- `shop_watch.py` runs every 3 hours on its own workflow and writes
  `docs/shop_data.json` (genuine markdowns at Shopify retailers)
- `docs/index.html` (GitHub Pages) shows both, with category tabs, Show
  filters (BNWT / New today / Bargains / Starred / My size / Off-menu),
  sort, brand and size pickers. The category, Show filters and sort you pick
  are remembered in that browser, so ticking BNWT once sticks
- New finds go to your phone via ntfy.sh: a few pushes a day at most for
  the really good ones (clothing only by default), and the rest of the
  best in one morning digest - see **Notifications**

## November 2026 review: why the feed wasn't working

- **The scan barely ran.** The workflow said every 20 minutes, but GitHub's
  scheduler only started it about 4 times a day (808 runs, all successful,
  4-5 a day). Good BNWT pieces usually sell within hours, so they'd gone
  before the feed saw them. The dashboard now says how old the scan is (in
  red once it's over 90 minutes), and the README explains how to trigger it
  reliably from outside.
- **Half the watches were missing at any time.** To cut requests, each scan
  ran only half of the digest watches, alternating - but the dashboard only
  shows the latest scan, so the other half vanished (51 of 100 watches were
  absent on 10 Nov). Every scan now runs every watch, and a watch whose
  request fails keeps its cards from the previous scan.
- **It kept seeing the same 40 listings.** Searches used Vinted's
  "relevance" order, so brand-new listings often never showed up. Brand
  watches now ask for the newest listings first and only add the relevance
  page when the newest one is mostly other brands (Vinted's text search is
  loose). Generic searches (vinyl, "side adjuster trousers") stay on
  relevance.
- **The score couldn't tell items apart.** Discount was measured against a
  guessed brand RRP, which made almost everything look 55-60% off: 121 of 150
  clothing cards scored 80-99, and a worn £16 hoodie ranked with BNWT
  finds. The score is now led by condition (BNWT first), price under your
  own cap for that search, and how recently it turned up. See **The ranking**.
- **Digests had never been sent.** The scan never committed the digest
  queue file, so the separate digest job found nothing and failed every
  morning. The digest is now sent by the scan itself, and its state lives in `seen_ids.json`, which every scan commits.
- **Notifications were noisy where they did fire.** Every new item on an
  instant watch pinged (worn ones included), and the urgent "exceptional"
  alert fired at a score most items reached. Now there's at most one push
  per scan, five a day, none overnight, and the rest goes in the digest.
- **Brand check was too loose one way.** A listing tagged "Drake" (rapper
  merch) matched Drake's, because a tag that was *part of* the brand name
  counted. Now the tag has to contain the brand; spellings go in
  `brand_match`.
- `seen_ids.json` had grown to 180k ids / 4MB. It now records when each item
  was first seen (for freshness) and forgets items not seen for 30 days.
- The Sell List page, `sell.json` and `price_check.py` were removed.

## The ranking

Every listing gets a score; the feed is sorted best-first. Weights are in
`config.json` under `scoring`, so they can be tuned without touching code.

| Signal | Points |
|---|---|
| Condition: new with tags / new without tags / very good / good | +45 / +32 / +10 / 0 |
| Price vs **your cap for that search** (`price_to`): anywhere from 15% to 50% of the cap | +30 |
| ...tapering to nothing at the cap (e.g. 75% of the cap) | +15 |
| ...under 15% of the cap (almost always a different, cheaper product, or a fake) | 0 |
| First seen in the last 6h / 24h / 3 days / 7 days | +20 / +14 / +8 / +3 |
| Still there after 14 days (usually overpriced) | -10 |
| Price dropped since last seen | +10 |
| Size stated and it's yours (size-less things like bags get 0) | +6 |
| Caution-tier brand, per flag raised | -20 each |

So a BNWT item found this morning at 70% of your cap scores about 83; the
same item a fortnight later scores about 53; a "very good" one at half your
cap found today scores about 66.

`rrp` is no longer part of the score. It's still used for the caution-tier
checks (an implausibly low price vs RRP is excluded) and to suggest a
`price_to` when a watch doesn't set one.

"Freshness" is when *this scan* first saw the listing - Vinted's search no
longer says when something was listed. With the scan running every 20
minutes and newest-first searching, that's within 20 minutes of it going up.

Every clothing watch keeps at least one slot, and no watch gets more than
`max_in_feed_default` (8) unless it sets its own `max_in_feed`. Electronics
subcategories (Games & Music, Sports, Baby, Tech, Caution) are each ranked
separately, so a busy one can't crowd out a quiet one.

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

## Deliberately read-only dashboard (plus an optional Scan now button)

The page has no settings and no write access to anything by default. All
configuration lives in `config.json` in this repo, protected by your GitHub
login. To change anything (brands, sizes, price ceilings, notification
tiers), edit `config.json` on GitHub and commit; the next scan picks it up.
The browser only stores your stars, your filter choices and, if you set it
up, the scan token below - none of it leaves the browser.

### Scan now

The **Scan now** button starts the "Vinted scan" workflow straight away, then
waits for the new results (about 5-8 minutes, plus a minute for GitHub Pages
to publish) and reloads the feed by itself.

It needs a GitHub token saved in that browser: the first tap asks for one.
Use a fine-grained token limited to this repo with only **Actions: Read and
write** (the same kind as the cron-job.org trigger; it can start and cancel
workflow runs, but can't read or change any code). It's stored in that
browser's local storage, never in the page or the repo. "Scan token
settings" in the footer lets you replace or forget it. Without a token, the
dialog links to the workflow's page on GitHub, where **Run workflow** does
the same thing.

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

## Reliable scan timing

GitHub's scheduler is best-effort, and for this repo the "every 20
minutes" schedule ran about 4 times a day. The workflow's schedule is still
there (at odd minutes, which helps a little), but to actually get a scan
every 20 minutes, have a free external cron service start it:

1. **Make a token.** GitHub → Settings → Developer settings → Personal
   access tokens → Fine-grained tokens → Generate. Repository access: only
   `bargain-watch`. Permissions: **Actions: Read and write** (nothing else).
   Set an expiry you're happy with and copy the token.
2. **Create the job** at cron-job.org (free account):
   - URL: `https://api.github.com/repos/em1ten/bargain-watch/actions/workflows/scan.yml/dispatches`
   - Schedule: every 20 minutes
   - Advanced → Request method: `POST`
   - Headers: `Accept: application/vnd.github+json`,
     `Authorization: Bearer <your token>`,
     `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main"}`
3. Save and use "Test run": it should return 204, and a new "Vinted scan"
   run appears in the Actions tab.

The workflow has a concurrency group, so if a scan is still running when the
next trigger arrives, the new one waits instead of overlapping. Optionally
do the same for `shops.yml` every 3 hours.

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
- `my_sizes` — your sizes per category (`denim` / `tops` / `smart` /
  `footwear` / `baby`); listings in other sizes are dropped
- `bargain_max_price` — flat "always a bargain" price cutoff
- `bargain_ceiling_ratio` — how far under a watch's own `price_to` a listing
  must be to count as a "Bargain" (default 0.6, i.e. bottom 60% of the price
  range you already said you'd consider)
- `feed_size` — how many listings the ranked feed keeps
- `max_in_feed_default` — most listings any one watch can have in the feed
  (8); a watch's own `max_in_feed` overrides it
- `scoring` — score weights (see **The ranking**)
- `notifications` — push threshold, daily push limit, quiet hours, digest
  time and size (see **Notifications**)
- `relevance_fallback_min` — if the newest-first page gives fewer than this
  many usable items for a brand watch, the relevance page is fetched too (6)
- `global_exclude` — junk keywords filtered out of every search

Per watch (in `watches`):
- `name`, `search_text` — label and what's searched on Vinted
- `price_to` — price ceiling for the search
- `rrp` — rough typical retail price; only used for the caution-tier checks
  and to suggest a `price_to` when none is set
- `brand_match` — the brand tag(s) a listing must carry (defaults to the
  watch name); add other spellings here
- `size_category` — which of `my_sizes` applies (`denim`/`tops`/`smart`/`footwear`/`baby`; `smart` covers collar sizes, waists and L/M for brands that do shirts, trousers and jackets).
  A listing that states a size outside that list is dropped, so keep the list generous
- `exclude_size` — size labels to drop (baby watches drop newborn up to 6 months)
- `exclude` — extra junk keywords for just this watch
- `notify` — `push`, `digest` or `off` (default: `push` for clothing,
  `digest` for everything else; the old `instant` means `push`)
- `order` — force `newest_first` or `relevance` for this watch

## Notes and limits

- Uses Vinted's public search endpoint (the same one their website calls),
  not an official API — it could change without warning, needing a small
  script fix. Normal for tools like this.
- Every 20 minutes at best (see **Reliable scan timing**), not real-time,
  to avoid hammering Vinted.
- Domain set to `www.vinted.co.uk` — change in `config.json` for other
  countries.
- For personal use finding items, not reselling automation.

## eBay (Technology / Games / Music)

**Not currently running:** the scan workflow doesn't call `ebay_watch.py`, so nothing below happens until a step for it is added back to `.github/workflows/scan.yml`.

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
every retailer does. Currently configured for ten shops (see `shop_watches`) — add more to `shop_watches` in
`config.json` if you find other shops you like that are also on Shopify
(`{domain}/products.json` returning real product data is the quick way to
check).

No secrets or setup needed for this one — Shopify's product feed is
public. Cards are tagged "· retail" and appear under the "Shops" pill,
alongside Technology/Games/Music/Football.

### How the shop scan works (rebuilt October 2026)

- **Its own workflow.** `.github/workflows/shops.yml` runs every 3 hours
  (shop prices rarely change); the 20-minute Vinted workflow no longer
  runs it. Both use a concurrency group and `git pull --rebase` before
  pushing, and the shop workflow commits only `docs/shop_data.json` and
  `shop_seen_ids.json`.
- **Whole catalogue.** It pages through every product (250 a page, up to
  60 pages) instead of the first 1,250. A failed page keeps what was
  already fetched and logs it to `errors`; hitting the page cap logs
  "catalogue truncated".
- **Price history.** `shop_seen_ids.json` is now version 2: every in-scope
  product (on sale or not) is recorded each scan, storing change points
  only (`prices: [[scan, price], ...]`, plus `low` / `high` /
  `first_seen`). Written one item per line with sorted keys, so unchanged
  items make no git diff. The old `{id: price}` file is migrated
  automatically on the first run. **If you ever roll back, restore
  `shop_seen_ids.json` from before that run too.**
- **Scoring** leans on verified signals: new all-time low (+45), dropped
  since last scan (+20), below its own typical price (x0.4, max 50),
  how many scans it has been watched (x0.25, max 40), the retailer's
  discount (capped at 60, x0.35), new (+3), off-menu (+10). Cards also
  carry `at_low`, `was_price_seen`, `observations` and `typical_price`.
  "Lowest seen" on a card means it is at or below every price recorded
  since the tracker started watching it.
- **Your size.** Each available on-sale variant title is split on `" / "`
  and compared (case-insensitively) with everything in `my_sizes`; any
  match gives the tick.
- **Feed mix.** Colour variants of the same product (`Name - Navy`,
  `Name - Olive`) take one slot; at most `shop_max_per_brand` (2) per brand
  within a shop (single-brand shops like Universal Works are exempt);
  at most `shop_max_per_shop` (20) per shop; `shop_feed_size` (60) overall.

### Off-menu finds

Shopify keeps "buyable" separate from "listed in collections", so shops
pull old-season leftovers out of their menus and Sale pages but leave them
live and orderable. Those are the least-competed deals. A marked-down item
is flagged **Off-menu** (badge on the card, and an "Off-menu" filter) when:

- it isn't in any of the shop's `sale_collections` (list the handles
  behind the site's Sale menu), or
- it carries one of the shop's `off_menu_tags` (Universal Works:
  `ARCHIVE`).

A sale collection with fewer than 5 products is ignored (Universal Works'
`/collections/sale` returned 0), so tags are used instead. To find the
right handles for a shop, run the **Shop scan** workflow manually with
"list_sale_collections" ticked and read the log, or run
`python shop_watch.py --list-sale-collections` locally. Until a shop has
`sale_collections` or `off_menu_tags` set, nothing from it is flagged.

If `NTFY_TOPIC` is set, an off-menu item that is in your size and at its
lowest seen price sends one push (once per item). Archived stock is
sometimes phantom, so expect the odd cancelled order.

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

`seen_ids.json` stores, for each listing, its last price, when the scan
first saw it, and the day it was last seen. A price drop on something
already seen gets a "was £X" badge, +10, and counts as notify-worthy like a
new listing. Trivial changes (under 1% or 50p) don't count. Items not seen
for 30 days are forgotten, so the file stays small.

## Notifications

Built not to bombard you. Needs `NTFY_TOPIC` set as a repo secret (setup
step 4). Settings are under `notifications` in `config.json`.

- **Pushes** (only from watches whose `notify` is `push` - every clothing
  watch by default): a new or price-dropped item scoring at least
  `push_min_score` (90 - in practice a BNWT item found in the last few hours,
  well under your cap). Everything worth a push in one scan goes out as
  **one** notification, with buttons to open up to three of the listings.
  At most `max_pushes_per_day` (5) a day, and none during `quiet_hours`
  (22:00-07:00 UK); anything held back goes into the digest instead.
- **Morning digest** (first scan after `digest_hour`, 08:00 UK): the best of
  everything since the last digest that scored at least `digest_min_score`
  (70), grouped into Clothing (top 10) and each other section (top 3 each:
  Baby, Games & music, Football, Tech, Caution).
- Baby, games, music, football, tech and caution-tier watches default to
  `digest` (never a push). Set any watch's `notify` to `push`, `digest` or
  `off` to change that.

Replaying the real 15:34 scan on 10 Nov (85 new listings in 88 minutes)
through these rules gives one push (a BNWT Reiss cashmere scarf at £20) and
29 items queued for the digest.

The push count, the digest queue and when the digest last went out are
stored at the top of `seen_ids.json`, which every scan commits.

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

## Discovery (removed)

The daily discovery-brand rotation, its "discovery" tag and the "+ Add to my watches" button were removed in October 2026 to cut Vinted request volume and keep the feed reliable. To track a new brand, add it to `watches` in `config.json`.

## "Other" (games, music, homeware, etc.) — tracked separately, not mixed into the main feed

Watches tagged `"category": "electronics"` in `config.json` (Film Cameras,
Hi-Fi & Turntables, Yeti) never appear in the default feed or any of the
other filters — they're only visible under the dedicated **"Other"**
pill. Scoring and notifications all work the same way
for them, they just don't get ranked alongside jackets and jeans. Any new
non-clothing watch just needs that same `"category": "electronics"` field
to stay out of the main feed.
