# Adventure Map

*NACIS Adventure Map Framework*

A self-hosted map of your photos, GPS tracks, hikes, drives and saved places, grouped
into trips. You can publish it read-only on any static host, or run the full version
on Cloudflare behind a login, where you can also edit places, trip names and photo
locations from the map.

This repository has the code and a small set of demo data. You add your own photos and
tracks.

## Features

- Geotagged photos from a folder, including 360° panoramas, shown on the map with a
  filmstrip of the photos in view.
- GPS activities from any watch or app: drop FIT, GPX or TCX files (Coros, Garmin,
  Suunto, Polar, Wahoo, Strava exports, phone apps) in a folder. Garmin Connect can
  also be synced directly. Google Takeout location history and Timeline exports are
  imported once.
- Hikes and drives matched to the OpenStreetMap trail and road network.
- Trips: days away from home grouped together, with dates, distance on foot, high
  point, photos, roads driven and a name.
- Saved places (POIs), drawn on top of everything else.
- The OpenFreeMap Liberty basemap, which needs no API key, with optional hillshade,
  satellite imagery and an OpenStreetMap trails overlay. You can add your own styles
  (see below).
- Optional Claude API features: photo captions and tags, hiding accidental shots,
  flagging private photos, suggesting locations for photos without GPS, and naming
  tracks and trips. The Batches API keeps the cost of a whole library low.
- Photo ids listed in `data-pipeline/private_photos.txt` are left out of every upload.

With editing on (Cloudflare only, see below) you can also add, move and rename places,
rename trips, mark trails as hiked, place photos that have no GPS by dragging them onto
the map, and hide photos.

## Architecture

[docs/architecture.html](docs/architecture.html) is a one-page diagram of the full
Cloudflare setup.

```
your photos, FIT/GPX/TCX files, Garmin, Google Takeout
        |
        v
data-pipeline/  (Python on your computer, nightly via Task Scheduler)
   scan + photo copies -> GeoJSON -> PMTiles (GDAL) -> trips -> AI naming/tagging
        |
        v  upload.py                            AM_DEPLOY_CMD
photos, tiles, data                   +   web/ (the site)
(a folder in web/, or a bucket)           on any static host, or Cloudflare Pages
                                          + D1 for edits, behind Cloudflare Access
        |
        v
web/  (MapLibre GL JS, no build step) in your browser
```

- `web/` is the site, in plain JavaScript with no bundler. `site.js` holds the two
  settings that change between hosts. `config.js` lists the data layers and basemap
  styles. `functions/` holds the Cloudflare Pages Functions, used only on Cloudflare:
  `/media/*` serves files from R2, and `/api/*` reads and writes D1.
- `data-pipeline/` holds the Python scripts that turn your sources into the site's
  data. [Data pipeline](#data-pipeline) below covers each step.
- `worker/schema.sql` defines the D1 tables.

## Running the demo locally

You need Python 3.10 or later.

```
cd data-pipeline
python make_demo_data.py          # writes a few demo places and one demo trip to out/
python dev_server.py 8789         # a static server with HTTP range support
```

Open <http://localhost:8789/web/index.html>. On localhost the app reads from
`data-pipeline/out/` instead of the published copy. Open the Trips panel (the compass
button) and pick the demo trip.

## Hosting

Two settings in `web/site.js` decide how the site works, and two files in
`data-pipeline/` tell the pipeline where to put things:

| | read-only, any host | read-only, bucket | Cloudflare with editing |
|---|---|---|---|
| `site.js` `mediaBase` | `'media'` | the bucket's public URL | `'/media'` |
| `site.js` `editing` | `false` | `false` | `true` |
| `storage.env` | `STORAGE=folder` | `STORAGE=s3` | `STORAGE=s3` (R2) |
| `site.env` `AM_EDITING` | `0` | `0` | `1` |
| places come from | `places.geojson` | `places.geojson` | the D1 database |

**Read-only on any static host** (GitHub Pages, Netlify, Vercel, Cloudflare Pages, your
own web server). `upload.py` copies photos, tiles and data into `web/media/`, and you
publish the `web/` folder. Set `AM_DEPLOY_CMD` in `site.env` to the command that
publishes it, for example `netlify deploy --prod --dir .`, and the nightly run will
call it. Most static hosts limit file counts or total size (GitHub Pages: 1 GB), so
this suits a few thousand photos. On Cloudflare Pages without R2, delete
`web/functions/` so the Functions don't take over `/media`.

**Read-only with a bucket** (AWS S3, Backblaze B2, Wasabi, Cloudflare R2 with public
access, MinIO). For bigger libraries. Fill in the `S3_*` settings in `storage.env`, make
the bucket publicly readable, and allow your site's origin in its CORS rules (GET and
the `Range` header, which PMTiles needs). Set `mediaBase` to the bucket's public URL.

**Cloudflare with editing.** Photos and data in a private R2 bucket, served through the
Pages Functions, your edits stored in D1, and Cloudflare Access in front of all of it:

- Create an R2 bucket (for example `photos`) and an R2 API token with Object Read
  & Write. Put the keys in `storage.env` (`S3_ENDPOINT` is
  `https://<account-id>.r2.cloudflarestorage.com`).
- Create a D1 database with `npx wrangler d1 create adventure-map`, then run
  `npx wrangler d1 execute adventure-map --remote --file worker/schema.sql`.
- Put the database id and bucket name in `web/wrangler.toml`.
- Create a Pages project. The first `npx wrangler pages deploy`, run from `web/`,
  creates it. Put its name in `site.env` as `AM_PAGES_PROJECT`.
- In Cloudflare Access (Zero Trust > Access > Applications), add a self-hosted app
  for both `your-project.pages.dev` and `*.your-project.pages.dev` (the preview
  deployments), with a policy that allows only your email. Do this before you
  upload any photos.

**Private or public.** A map of your photos shows where you live and travel. If the
site is private (`AM_PRIVATE=1` in `site.env`, the default), put it behind a login:
Cloudflare Access, Netlify or Vercel password protection, or basic auth on your own
server. The nightly run then checks that the site still refuses anonymous visitors and
logs an alert if it doesn't. For a public map set `AM_PRIVATE=0`, and think about
leaving out photos taken at home.

## Setting up your own copy

1. Install the tools. Setup is written for Windows; the Python scripts also run on
   macOS and Linux, but the nightly task is set up with PowerShell. You need Python
   (via [uv](https://docs.astral.sh/uv/)) and QGIS or OSGeo4W for GDAL's `ogr2ogr`,
   which writes the PMTiles. Node.js LTS is needed only for Cloudflare's `wrangler`.
   Then run `powershell -ExecutionPolicy Bypass -File data-pipeline/setup.ps1`.
2. Pick a hosting option above and set `web/site.js` to match.
3. Copy the `.example` files in `data-pipeline/` and fill them in: `site.env` (photo
   folder, activities folder, deploy command, site URL), `storage.env` (where the
   files go), and optionally `.garmin.env` and `.anthropic.env`. All of them are
   gitignored.
4. Describe your history in `data-pipeline/homes.json`: where you lived and when, since
   trips are the days you were away, plus any long trips you want to name yourself.
   Add the ids of photos you never want published to
   `data-pipeline/private_photos.txt`. For a read-only site, add your places to
   `data-pipeline/places.geojson` (QGIS or <https://geojson.io> work well).
5. Put your activity files in `data-pipeline/activities/` (or the folder named in
   `AM_ACTIVITIES_DIR`).
6. Run the first import. `python auto_import.py` scans your photo folder, imports
   activities, builds the tiles, publishes the files and deploys the site. To add
   Google Takeout data, see [Bringing in your history](#bringing-in-your-history).
   Then run `register_task.ps1` to make the import run every night.

## Data pipeline

The Python scripts in `data-pipeline/` turn your sources into the map's data. Run them
from that folder with the venv `setup.ps1` creates. Everything lands in `out/`
(GeoJSON, GeoJSONSeq, PMTiles, trips) and `derivatives/` (web-sized photos). On
localhost the app reads those folders directly, so you can try every step before
anything is published.

### Settings files

All gitignored except the last three. Copy the `.example` files and fill them in.

| file | what for |
|---|---|
| `site.env` | photo and activity folders, read-only or editing, deploy command, site URL |
| `storage.env` | where photos, tiles and data are published: a folder or an S3-compatible bucket |
| `.garmin.env` | optional: Garmin Connect login |
| `.anthropic.env` | optional: Claude API key for tagging and naming |
| `homes.json` | where home was, and when. Trips are the days away from it |
| `private_photos.txt` | photo ids that must never be published |
| `places.geojson` | your places, for read-only sites (editing sites keep them in D1) |

### The nightly run: `auto_import.py`

```
python auto_import.py               # every step below that has new work
python auto_import.py --no-deploy   # build and upload, don't publish
python auto_import.py --skip-scan   # just rebuild/upload/deploy what's local
```

1. **Scan** the photo folder (`AM_PHOTO_DIR`) for new geotagged photos, make
   thumbnail, web and panorama copies, and catalogue photos without GPS.
2. **Tag** new photos with Claude, if `.anthropic.env` exists: caption, tags,
   quality, "keep" (screenshots and accidents are hidden), and "sensitive" (added to
   `private_photos.txt` and any published copies deleted).
3. **Upload** new photo copies (`upload.py photos` skips what's already published).
4. **Activities**: new FIT/GPX/TCX files in the activities folder
   (`import_activities.py`), plus Garmin Connect if you've logged in once
   (`garmin_sync.py`). Both add to `out/activities.geojsonl`.
5. **Places**: read-only sites publish `places.geojson`; editing sites back up the
   places stored in D1 (`export_pois.py`).
6. **Tiles**: `build_pmtiles.py` turns the layers into PMTiles, and `upload.py tiles`
   publishes them under content-hashed names with a `data/tiles.json` index, so an
   open page never mixes old and new files.
7. **Trips**: `build_trips.py`, `name_tracks.py`, `name_trips.py`, then
   `upload.py trips`.
8. **Deploy** with `AM_DEPLOY_CMD` (or `wrangler pages deploy` when
   `AM_PAGES_PROJECT` is set) and, for a private site, check that it still refuses
   anonymous visitors.

`register_task.ps1` schedules it daily in Windows Task Scheduler. Each run logs to
`logs/`.

### Bringing in your history

- **Activity files** (FIT, GPX, TCX, or `.gz` copies) from any watch or app: put them
  in `activities/` (or `AM_ACTIVITIES_DIR`) and run `python import_activities.py`, or
  let the nightly run do it. Files are recognised by content, so renaming or
  re-exporting one doesn't import it twice. Activities without GPS (treadmill, pool)
  are skipped.
- **Garmin Connect**: `python garmin_sync.py login` once (it handles MFA), then the
  nightly run keeps it current.
- **Google location history** (Takeout, Location History or Timeline):
  `python import_google.py path\to\Timeline.json` for the new Timeline export, or
  `python convert_google_timeline.py INPUT.json --outdir out --raw-signals`.
- **GPX tracks as hikes** (routes you want in the Hikes layer rather than
  Activities): `python import_gpx.py path\to\gpx-folder --write`, then
  `python build_pmtiles.py hikes`. Leave out `--write` for a dry run.
- **Snap tracks to trails and roads** (optional): `python snap_regions.py all`
  downloads Overture road segments for each region with DuckDB and map-matches the
  tracks to them, then `python build_pmtiles.py hikes_snapped`.

### Trips

`build_trips.py` decides, day by day, whether you were home or away: any photo, track
or location point farther than `AWAY_KM` from the home in effect counts as away.
Consecutive away days form a trip. Going home in between, or a jump between far-apart
places across days with no data, splits them. Each trip gets an outline of where it
happened (used by styles with `tripArea`), photos, tracks, distance on foot, high point
and, for multi-day trips, the roads driven. `name_tracks.py` names tracks after the
OpenStreetMap trails they follow, and `name_trips.py` names and summarises trips and
picks a hero photo. Both use Claude and cache the results, so re-runs cost nothing.
Renames made in the app win. Set `AM_TRIPS_NEED_PHOTOS=1` in `site.env` to leave
trips without photos out of the panel.

### Photos without GPS

- `suggest_geotags.py` suggests a spot for each one from your tracks at the same time.
- `geotag_ai.py` asks Claude where a photo was taken from what's in it (signs,
  landmarks, scenery) and the trip you were on that day.
- With editing on, you place them from the app's geotag gallery, and
  `apply_geotags.py` writes the coordinates back into the original files' EXIF.

### Claude costs (all optional)

- `tag_photos.py submit` and `collect` run the whole library through the Batches API
  at half price. Try `tag_photos.py trial` first; it writes a review page.
- `tag_photos.py new` runs nightly on new photos only.
- `name_trips.py`, `name_tracks.py` and `geotag_ai.py` cost cents per run.

### Other tools

`upload.py purge-private` deletes published copies of photos in `private_photos.txt`.
`export_pois.py` backs up your D1 edits. `dedupe_photos.py` finds duplicate photos.
`fix_orientation.py` re-makes copies for photos with unusual EXIF rotation.
`stamp_assets.py` adds cache-busting `?v=` stamps to the site's JS and CSS (deploy
runs it). `dev_server.py` is a local server with HTTP range requests.

## Adding a basemap style

Each entry in `styles` in `web/config.js` adds a choice to the Style menu. An entry can
be a style URL (`url`), an inline MapLibre style object (`style`), or a `build()`
function that returns a style object, so a style can be written entirely in code.
These optional settings change how the app works with a style:

| option | what it does |
|---|---|
| `dataBefore` | layer id your data is inserted under, for example to keep labels on top |
| `dataLinesBefore` | a second insertion point for lines only, for example under a screen effect |
| `paintLayer` | a custom WebGL layer that post-processes the frame drawn below it |
| `onLoad(map)` | runs after the style loads, to add images or listeners |
| `layerColors` | colours for your data layers in this style |
| `decorateLine` | extra layers drawn with a data line, such as casings or shadows |
| `terrain3d`, `exaggeration` | 3D terrain from a raster-dem source in the style |
| `hasRelief`, `contoursOff` | the style has its own relief, or starts with contours off |
| `focus` | connects the panel's Blur distance slider to the style |
| `tripArea(map, trip)` | called with the trip's outline when a trip is opened or closed |
| `font`, `attribution` | the fontstack your glyph server serves, and the credits to show |

Your places stay on top of anything a style adds.

## Costs

A read-only site on a static host is usually free. On Cloudflare, the free tier covers
Pages, D1 and Access for one person, and R2 is free up to 10 GB of storage. The web
copies of a large photo library take a few GB. The Claude API is pay as you go: tagging
tens of thousands of photos with the Batches API costs a few dollars, and naming trips
and tracks costs cents. You can skip the AI features entirely.

## About this copy

This is a snapshot of my own map's code, shared after a NACIS talk. I don't plan to
maintain it or add new features here, but you're welcome to fork it and make it yours.

## Data and credits

Basemap: [OpenFreeMap](https://openfreemap.org) Liberty, © OpenStreetMap contributors.
Hillshade and trails overlay: [OpenStreetMap US](https://tiles.openstreetmap.us).
Imagery: Esri. Map rendering: [MapLibre GL JS](https://maplibre.org). Geocoding:
Nominatim and Photon (komoot). The map's attribution credits each source.

## License

MIT. See [LICENSE](LICENSE).
