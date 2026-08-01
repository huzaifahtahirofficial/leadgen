# Nestick frontend on Vercel

This folder deploys the **static control-panel UI**. The scraping engine + API
live on Render (`../render`) and the UI calls it cross-origin.

## Why a build step

The UI is plain static files from `nestick/web/static/`. `build.js` copies
them into `dist/` and injects a `config.js` that tells the frontend where the
Render API is (`window.NESTICK_API_BASE`). Without it the UI would look for
the API on the Vercel domain and find nothing.

## Deploy

1. Deploy the Render backend first and note its URL
   (e.g. `https://nestick.onrender.com`).
2. On Vercel, **Add New → Project**, import the repo.
3. Set **Root Directory** to `deploy/vercel`.
4. Add the environment variable:
   - `NESTICK_API_BASE` = `https://<your-render-service>.onrender.com`
5. Vercel reads `vercel.json`: build `node build.js`, output `dist`.
   (No framework, no package.json install needed.)
6. Deploy.

## How the API keys flow

API keys are typed into the UI and POSTed to the Render backend, which stores
them in `~/.nestick/config.json` (or `NESTICK_CONFIG_DIR` on the persistent
disk). They never touch Vercel.

## Login

If the Render service has `AUTH_MONGODB_URI` + `JWT_SECRET` configured, the UI
shows a sign-in page and sends the returned JWT as `Authorization: Bearer …`
on every API call (kept in localStorage). If auth is not enabled, the UI runs
without a login screen.

## Security note

The Render backend allows cross-origin calls from `.vercel.app`, `.onrender.com`
and `skelersecurity.app` via its CSRF allow-list. With auth enabled, that is a
real gate; without it, anyone with your UI URL could start scrapes.
