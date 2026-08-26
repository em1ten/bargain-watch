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

## config.json reference

Top-level:
- `my_sizes` — your sizes per category (`denim` / `tops` / `footwear`);
  powers the size flag and score bonus
- `bargain_max_price` — flat "always a bargain" price cutoff
- `bargain_threshold_pct` — % below `rrp` that counts as a bargain
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
