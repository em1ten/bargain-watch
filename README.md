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
  filter pills: All / My size / New / Bargains / Discovery / Starred
- New finds push to your phone via ntfy.sh — instantly for priority brands,
  bundled into one daily digest for the rest (`digest_send.py`)

## The ranking

Every listing gets a score. Higher = better find, feed is sorted best-first:

| Signal | Points |
|---|---|
| Discount vs typical retail price (`rrp`) | up to 60 |
| At/under the flat bargain price (default £20) | +15 |
| Condition (new with tags → good) | +15 → +3 |
| Established, well-rated seller | +5 |
| Matches your size | +20 |
| New since the last scan | +5 |

## Discovery

`discovery_pool` in `config.json` holds adjacent brands worth knowing
(Arpenteur, A.P.C., Our Legacy, Stan Ray, Margaret Howell, and more). Each
day, 3 rotate in automatically and get scanned alongside your core watches —
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
