# Adventure Map

*NACIS Adventure Map Framework*

A private, self-hosted map of everywhere you've been: your photos, GPS tracks,
hikes, drives and places, organised into trips.

This is a framework, not a product. It ships with the code and a little demo data;
you bring your own photos and tracks. Everything runs on your own Cloudflare
account (free tier is enough for most people) behind a login only you can pass.

## What it does

- **Photos on the map.** Scans a folder of geotagged photos, makes web-sized copies
  (including 360° panoramas) and plots them, with a filmstrip of what's in view.
  Photos without GPS go to a gallery where you drag them onto the map.
- **Tracks.** Garmin Connect activities sync nightly; Google Takeout location
  history and timeline exports import once. Hikes and drives can be snapped to the
  OpenStreetMap trail and road network.
- **Trips.** Days away from home are grouped into trips (homes.json says where home
  was, and when). Each trip gets a date range, distance on foot, high point, photos,
  the roads you drove, and a name. Rename any trip in the panel.
- **Basemap.** [OpenFreeMap Liberty](https://openfreemap.org) out of the box, key-free,
  with optional hillshade and satellite imagery underneath and an OpenStreetMap trails
  overlay. Bring your own styles - see *Adding a basemap style* below.
- **Your places (POIs)** - add, move and edit them on the map; always drawn on top.
- **Optional AI (Claude API).** Caption and tag photos, hide accidental shots, flag
  private photos, suggest locations for photos with no GPS from what's in them, and
  name tracks and trips from the trails and places they pass. Batch pricing keeps a
  whole library cheap; every step is optional.
- **Privacy built in.** Everything sits behind Cloudflare Access. A list of private
  photo ids is filtered out of every publish step, and the nightly job checks that
  the site still refuses anonymous visitors.

## How it fits together

The one-page cut sheet, [docs/architecture.html](docs/architecture.html), shows every
piece and how data moves between them.

```
your photos, GPX/Garmin, Google Takeout
        |
        v
data-pipeline/  (Python, runs on your computer, nightly via Task Scheduler)
   scan + derivatives -> GeoJSON -> PMTiles (GDAL) -> trips -> AI naming/tagging
        |
        v  upload_r2.py                         wrangler pages deploy
Cloudflare R2 (photos, tiles, data)   +   Cloudflare Pages (web/ + Functions)
                                          + D1 (your edits: POIs, geotags, trip names)
                                          all behind Cloudflare Access
        |
        v
web/  (MapLibre GL JS, no build step) in your browser
```

- `web/` - the site. Plain JavaScript, no bundler. `config.js` lists the data layers
  and basemap styles. `functions/` are the Cloudflare Pages Functions: `/media/*` serves
  R2 files (with HTTP range requests for PMTiles), `/api/*` reads and writes D1.
- `data-pipeline/` - Python scripts that turn raw sources into the site's data. See
  [data-pipeline/README.md](data-pipeline/README.md) for every step.
- `worker/schema.sql` - the D1 tables.

## Try it locally (5 minutes)

You need Python 3.10+.

```
cd data-pipeline
python make_demo_data.py          # writes a few demo places and one demo trip to out/
python dev_server.py 8789         # a static server with HTTP range support
```

Open <http://localhost:8789/web/index.html>. On localhost the app reads from
`data-pipeline/out/` instead of the cloud. Open the Trips panel (compass button) and
choose the demo trip.

## Adding a basemap style

Each entry in `styles` in `web/config.js` is one choice in the panel's Style menu.
A style can be a style URL (`url`), an inline MapLibre style object (`style`), or a
`build()` function that returns one - so a style can be written entirely in code.
Optional hooks let a style go further:

| option | what it does |
|---|---|
| `dataBefore` | layer id your data is inserted under (e.g. keep labels on top) |
| `dataLinesBefore` | a second anchor for lines only (e.g. under a screen effect) |
| `paintLayer` | a custom WebGL layer that post-processes the frame drawn below it |
| `onLoad(map)` | runs after the style loads (add images, listeners) |
| `layerColors` | per-style colours for your data layers |
| `decorateLine` | extra layers drawn with a data line (casings, shadows) |
| `terrain3d`, `exaggeration` | 3D terrain with a raster-dem source in the style |
| `hasRelief`, `contoursOff` | the style brings its own relief / starts with contours off |
| `focus` | wires the panel's Blur distance slider to the style |
| `tripArea(map, trip)` | called when a trip is opened or closed, with its outline |
| `font`, `attribution` | the fontstack your glyph server serves; credits to show |

Your POIs are always drawn on top of whatever a style adds.

## Make it yours

1. **Install the tools** (Windows; the Python scripts also run on macOS/Linux, but
   the nightly task setup is PowerShell): Python via [uv](https://docs.astral.sh/uv/),
   Node.js LTS (for `wrangler`), and QGIS or OSGeo4W (for GDAL's `ogr2ogr`, which
   writes the PMTiles). Then `powershell -ExecutionPolicy Bypass -File data-pipeline/setup.ps1`.
2. **Cloudflare.** Create an account, then:
   - an R2 bucket (e.g. `photos`) and an R2 API token with Object Read & Write;
   - a D1 database: `npx wrangler d1 create adventure-map`, then
     `npx wrangler d1 execute adventure-map --remote --file worker/schema.sql`;
   - put the database id in `web/wrangler.toml`;
   - a Pages project: the first `npx wrangler pages deploy` (run from `web/`) creates it;
   - **Cloudflare Access** (Zero Trust -> Access -> Applications): a self-hosted app
     for `your-project.pages.dev` AND `*.your-project.pages.dev` (preview
     deployments), with a policy that only allows your email. Do this before you
     upload any photos.
3. **Credentials** (all gitignored; copy the `.example` files in `data-pipeline/`):
   `.r2.env` (R2 keys), `site.env` (your site URL, Pages project, photo folder),
   optionally `.garmin.env` and `.anthropic.env`.
4. **Your history.** Edit `data-pipeline/homes.json`: where you lived and when (trips
   are days away from home), plus any long trips you want named by hand.
   Add photo ids you never want published to `data-pipeline/private_photos.txt`.
5. **First import.** `python auto_import.py` scans your photo folder, builds the
   tiles, uploads to R2 and deploys. Add Google Takeout or old GPX tracks with the
   steps in the pipeline README. Then `register_task.ps1` makes it nightly.

## Costs

Cloudflare's free tier covers Pages, D1 and Access for one person; R2 is free up to
10 GB stored. A large photo library's web copies run a few GB. The Claude API is
pay-as-you-go: tagging tens of thousands of photos with the Batches API costs a few
dollars; naming trips and tracks costs cents. Everything AI is optional.

## Data and credits

Basemap: [OpenFreeMap](https://openfreemap.org) Liberty, © OpenStreetMap contributors.
Hillshade and trails overlay: [OpenStreetMap US](https://tiles.openstreetmap.us).
Imagery: Esri. Map rendering: [MapLibre GL JS](https://maplibre.org). Geocoding:
Nominatim / Photon (komoot). Each source is credited in the map's attribution.

## License

MIT - see [LICENSE](LICENSE).
