-- Editable life POIs (campsites, peaks, trailheads, ...).
CREATE TABLE IF NOT EXISTS pois (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  type       TEXT,
  lon        REAL NOT NULL,
  lat        REAL NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pois_type ON pois(type);

-- In-app geotagging of previously un-geotagged photos.
--   status: 'placed' (has lon/lat) | 'skipped' (not taggable) | 'applied' (EXIF written)
CREATE TABLE IF NOT EXISTS photo_geotags (
  id         TEXT PRIMARY KEY,          -- stable photo id (matches untagged manifest)
  lon        REAL,
  lat        REAL,
  status     TEXT NOT NULL DEFAULT 'placed',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_geotags_status ON photo_geotags(status);

-- Trail segments marked "hiked" (selected from the OSM trails overlay; no GPS needed).
CREATE TABLE IF NOT EXISTS hiked_trails (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  osm_id     TEXT,
  name       TEXT,
  geojson    TEXT NOT NULL,            -- the segment geometry as a JSON string
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Geotagged photos to hide from the map (filtered out client-side; reversible).
CREATE TABLE IF NOT EXISTS hidden_photos (
  id         TEXT PRIMARY KEY,         -- photo id to hide
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Trip names edited in the Trips panel; they win over trips.json's generated names.
-- start/end let the panel re-attach a name if a rebuild changes the trip id.
CREATE TABLE IF NOT EXISTS trip_names (
  id         TEXT PRIMARY KEY,         -- trips.json trip id
  name       TEXT NOT NULL,
  start      TEXT,                     -- trip start date (ISO)
  "end"      TEXT,                     -- trip end date (ISO)
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
