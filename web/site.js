// Settings for your copy of the map. These two values are all you need to change
// for a new host; see "Hosting" in the README.

window.SITE = {
  // Where the pipeline put your photos, tiles and data (the photos/, tiles/ and
  // data/ folders written by data-pipeline/upload.py):
  //   'media'        files published with the site (STORAGE=folder in storage.env):
  //                  GitHub Pages, Netlify, Vercel, any web server
  //   '/media'       Cloudflare Pages + R2, served through web/functions/media
  //   'https://...'  a public bucket or CDN that serves those folders
  mediaBase: 'media',

  // false: read-only. No server code or database is needed; your places come
  //        from data/pois.geojson (copied from data-pipeline/places.geojson).
  // true:  add and edit places, rename trips, mark hiked trails, geotag photos
  //        and hide photos from the map. Needs the /api backend: Cloudflare Pages
  //        Functions and a D1 database (see web/functions and worker/schema.sql).
  editing: false,
};
