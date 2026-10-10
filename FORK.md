# Ranked mode (fork of mscodemonkey/artwork-uploader-plex)

This fork adds **ranked mode**: a faster, ranking-aware way to keep Plex in step with a fixed list of
followed ThePosterDB users. Everything else behaves exactly as upstream.

## What it does

With **Ranked mode** switched on in Settings, a bulk run of a file of ThePosterDB user URLs works like this:

1. **Ranking = the order of the users in the bulk file.** The top line is the most preferred user.
2. **New uploads only.** Each user's cached index is brought up to date (the same incremental crawl as
   *Cache TPDb user pages*). No other ThePosterDB requests are made for posters it already knows.
3. **One listing per Plex library.** Every cached poster is matched to your libraries in memory, instead
   of searching every library in Plex for every poster on every run (the reason upstream runs took hours).
4. **Best poster per slot.** For each item, the main poster and *each season poster separately* come from
   the highest-ranked user who has one; within a user, their newest upload wins. When none of the users has
   a poster for something, it's left alone.
5. **Only changes are applied.** A slot whose winner is the same as last time costs nothing. A changed
   winner is written to the Kometa asset folder **and** uploaded to Plex immediately, so Plex updates at
   once and Kometa's next run applies the same file instead of reverting it.
6. **Nothing is deleted.** A poster file that gets replaced is moved to
   `config/replaced_assets/<run time>/<library>/<folder>/`.
7. **Seasons not in Plex yet** are saved to the asset folder (if *Stage assets* is on) and uploaded as soon
   as Plex has the season.

Sonarr/Radarr imports (the webhook) use the same ranking and the same apply step once ranked mode is live.

## Dry run

*Ranked mode dry run* is on by default. A dry run makes every decision, then writes a CSV report to
`config/ranked_reports/` (action, item, slot, chosen user, poster page link) and changes nothing in Plex or
in the asset folders. Nothing else changes anything either: lines in the bulk file that aren't ThePosterDB
user URLs are skipped (not run the normal way), and Sonarr/Radarr imports are logged and skipped.

The first dry run is slow (a one-off): to know whether a poster already in an asset folder is the winner, it
downloads each winner once to compare checksums. Checksums and poster-page TMDb IDs are remembered, so the
real run and every later run don't fetch them again.

## Matching rules

- Same title **and** same year as a Plex item: trusted.
- Title matches but the year is one out (or missing): the poster page on ThePosterDB is read once, only
  if that poster would win, to confirm the TMDb ID.
- A title that matches several different items (same-name remakes): the poster page is read once.
- If a poster page that's needed can't be read, the slot is left alone for that run rather than given to a
  lower-ranked user.
- An asset folder used by more than one different item in a library (films side by side in one folder, loose
  files in the library root) is never written to, since Kometa would put that poster on all of them. These
  show as `skip` in the report.
- A user listed twice in the bulk file keeps their first (higher) place.

## Writing files

The new poster is written in full first; only then is the old file moved to the backup folder. A backup never
overwrites an earlier backup (a second one in the same folder becomes `poster.1.jpg`, and so on).

## State

Ranked mode keeps its state in three tables in `config/asset_index.db`:
`ranked_asset_meta`, `ranked_applied`, `ranked_order`. Deleting those tables makes the next run treat
everything as new (it will compare against the asset folders again; nothing is lost).

## Docker image

`.github/workflows/fork-image.yml` builds `ghcr.io/<owner>/artwork-uploader:ranked` on every push to the
`ranked-mode` branch (after the tests pass), plus a `ranked-<commit>` tag for rolling back.
