# Vinted Watch

A small self-hosted tool that checks a list of Vinted searches on a schedule,
tracks which listings are new, and shows results on a simple dashboard page.
No third-party app, nothing paid, your data stays in your own repo.

- `config.json` — the searches to run (brand, keywords, price ceiling, excludes, condition)
- `vinted_watch.py` — does the actual checking, every 20 minutes
- `digest_send.py` — sends one daily summary for lower-priority ("digest") watches
- `.github/workflows/scan.yml` — runs the check on a schedule via GitHub Actions
- `.github/workflows/digest.yml` — sends the daily digest push
- `docs/` — the dashboard page (served via GitHub Pages), with a starrable watchlist

## Setup (10 minutes)

1. **Create a new GitHub repo** and push these files to it (`main` branch).

2. **Turn on GitHub Pages**
   Repo → Settings → Pages → Build and deployment → Source: "Deploy from a branch" →
   Branch: `main`, folder: `/docs` → Save.
   Your dashboard will appear at `https://<your-username>.github.io/<repo-name>/`
   after the first successful run (may take a couple of minutes to go live).

3. **Allow the Action to push updates**
   Repo → Settings → Actions → General → Workflow permissions →
   select "Read and write permissions" → Save.
   (This lets the scheduled job commit updated listings back to the repo.)

4. **(Optional) Set up push notifications with ntfy.sh**
   - Pick any topic name — anything unguessable, e.g. `vinted-yourname-8k2j` (topics
     on ntfy.sh are public if someone knows the name, so don't use anything obvious).
   - Install the [ntfy app](https://ntfy.sh/) (iOS/Android) or use the web app,
     and subscribe to that topic name.
   - In your repo: Settings → Secrets and variables → Actions → New repository secret →
     name it `NTFY_TOPIC`, value = your topic name.
   - Skip this step if you'd rather just check the dashboard page yourself —
     everything still works, you just won't get push alerts.
   - This one secret covers both instant pushes and the daily digest.

5. **Test it manually**
   Repo → Actions tab → "Vinted scan" → Run workflow. After it finishes,
   refresh your dashboard page — you should see results.

It'll then run automatically every 20 minutes from then on.

## Editing your searches

Open `config.json`. Each entry is one saved search:

```json
{ "name": "Nudie Jeans", "search_text": "nudie jeans", "price_to": 60 }
```

- `name` — label shown on the dashboard
- `search_text` — what gets typed into Vinted's search
- `price_to` / `price_from` (optional) — price range
- `notify` (optional) — `"instant"` (push right away), `"digest"` (bundled into
  one push a day), or `"off"` (dashboard only, no notification). Defaults to instant.
- `exclude` (optional) — extra keywords to filter out for just this watch,
  on top of `global_exclude`

Two settings apply across all watches:

- `global_exclude` — keywords filtered out of every search (this is what
  stops junk like "kids" or "replica" listings from a broad brand-name
  search — the exact problem you hit with "Levi's")
- `conditions` — only show listings with one of these condition labels
  (e.g. `["New with tags", "New without tags", "Very good"]`). Leave the
  list empty to allow any condition.

Add, remove, or edit entries, then commit — the next scheduled run picks up
the changes.

## Watchlist

Each card on the dashboard has a star in the top-right corner. Starring an
item saves it in your browser (not synced anywhere, not sent to GitHub) so
you can toggle "Starred only" at the top of the page to quickly get back to
things you've flagged, even as new listings push older ones down.

## Seller info

Where Vinted's search results include it, each card shows the seller's
username, feedback percentage, and review count under the price — a quick
gut-check before you click through. This depends on what Vinted's own
search response includes at the time, so it won't always be present for
every listing.

## Notes and limits

- This uses Vinted's public search endpoint (the same one the website itself
  calls), not an official API — Vinted could change it at any point, which
  would need a small script fix. That's normal for tools like this.
- Runs every 20 minutes rather than truly real-time, to stay well within
  GitHub Actions' free tier and avoid hammering Vinted's servers.
- Domain is set to `www.vinted.co.uk` in `config.json` — change it if you're
  shopping on a different country's Vinted site.
- This is for personal use finding items, not for reselling automation.
