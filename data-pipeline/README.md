# Adventure Map - data pipeline

Python scripts that turn your raw sources into the map's data. Everything lands in
`out/` (GeoJSON, GeoJSONSeq, PMTiles, trips) and `derivatives/` (web-sized photos);
`upload_r2.py` pushes those to Cloudflare R2 and `auto_import.py deploy` publishes the
site. Run them from this folder with the venv that `setup.ps1` creates.

On localhost the web app reads `out/` and `derivatives/` directly
(`python dev_server.py 8789`, then <http://localhost:8789/web/index.html>), so you can
try every step before anything goes to the cloud.

## Setup

```
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Creates a Python venv (`C:\uv\venvs\adventure-map`, outside any synced folder),
installs `requirements.txt`, checks `ogr2ogr` (QGIS / OSGeo4W), `wrangler` and your
credentials, and registers the nightly task. Copy the `.example` files and fill them in:

| file | what for |
|---|---|
| `site.env` | your site URL (privacy check), Pages project, photo folder |
| `.r2.env` | R2 API keys + bucket |
| `.garmin.env` | optional: Garmin Connect login |
| `.anthropic.env` | optional: Claude API key for tagging and naming |
| `homes.json` | where home was, and when - trips are days away from it |
| `private_photos.txt` | photo ids that must never be published |

## The nightly run: `auto_import.py`

```
python auto_import.py               # everything below that has new work
python auto_import.py --no-deploy   # build and upload, don't publish
python auto_import.py --skip-scan   # just rebuild/upload/deploy what's local
```

1. **Scan** the photo folder (`AM_PHOTO_DIR`) for new geotagged photos
   (`photos_ingest.py scan`), make thumbnail / web / panorama copies, and catalogue
   photos without GPS for the in-app geotagging gallery.
2. **Tag** new photos with Claude, if `.anthropic.env` exists (`tag_photos.py new`):
   caption, tags, quality, "keep" (screenshots and accidents are hidden), and
   "sensitive" (added to `private_photos.txt` and purged from R2).
3. **Upload** new photo copies to R2 (`upload_r2.py photos`, skips what's there).
4. **Garmin** sync, if `.garmin.env` exists (`garmin_sync.py sync`).
5. **Tiles**: `build_pmtiles.py` turns the GeoJSON layers into PMTiles (zoom-banded
   thinning for photos), `upload_r2.py tiles` uploads them under content-hashed names
   and publishes `data/tiles.json`, so open pages never mix old and new files.
6. **Trips**: `build_trips.py` -> `name_tracks.py` -> `build_trips.py` ->
   `name_trips.py` -> `upload_r2.py trips`.
7. **Deploy** the site and **verify** that it still refuses anonymous visitors (on the
   main hostname and a preview-deployment hostname).

`register_task.ps1` schedules it daily (Windows Task Scheduler); each run logs to `logs/`.

## Bringing in your history (one-off)

**Google location history** (Takeout -> Location History / Timeline):

```
python import_google.py path\to\Timeline.json      # new-format Timeline export
python convert_google_timeline.py INPUT.json --outdir out --raw-signals
```

**GPX tracks from any app or device** (Strava, Gaia, CalTopo, an old handheld GPS...):

```
python import_gpx.py path\to\gpx-folder            # dry run
python import_gpx.py path\to\gpx-folder --write    # -> out/hikes.geojsonl
python build_pmtiles.py hikes
```

**Snap tracks to trails and roads** (optional; downloads Overture road segments per
region with DuckDB, then map-matches): `python snap_regions.py all` ->
`out/hikes_snapped.geojsonl`, then `python build_pmtiles.py hikes_snapped`.

**Garmin Connect**: `python garmin_sync.py login` once (handles MFA), then
`python garmin_sync.py sync`. After that the nightly run keeps it current.

## Photos without GPS

- `suggest_geotags.py` - suggests a spot for each one from your tracks at the same
  time (nearest-in-time GPS point).
- `geotag_ai.py` - asks Claude where a photo was taken from what's in it (signs,
  landmarks, scenery) plus the trip you were on that day; shown as suggestions.
- Place them in the app's geotag gallery; `apply_geotags.py` then writes the
  coordinates back into the original files' EXIF and rebuilds.

## Trips

`build_trips.py` decides, day by day, whether you were home or away (any evidence -
photos, tracks, location history - farther than `AWAY_KM` from the home in effect).
Consecutive away days form a trip; going home in between, or a jump between far-apart
places across days with no data, splits them. Each trip gets an outline of where it
happened (for styles that use `tripArea`), photos, tracks, distance on foot, high point and, for multi-day
trips, the roads driven. `name_tracks.py` names tracks from the OpenStreetMap trails
they follow; `name_trips.py` names and summarises trips and picks a hero photo (both
use Claude, results cached so re-runs cost nothing). Renames made in the app win.

## AI costs (Claude, all optional)

- `tag_photos.py submit` / `collect` - the whole library through the Batches API at
  half price; a trial first: `tag_photos.py trial` writes a review page.
- `tag_photos.py new` - nightly, only new photos.
- `name_trips.py`, `name_tracks.py`, `geotag_ai.py` - cents per run, cached.

## Other tools

`export_pois.py` (back up your D1 edits), `dedupe_photos.py`, `fix_orientation.py`
(re-make copies for photos with unusual EXIF rotation), `stamp_assets.py` (cache-busting
`?v=` stamps on the site's JS/CSS; deploy runs it), `dev_server.py` (local server with
range requests).
