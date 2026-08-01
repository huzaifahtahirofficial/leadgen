# Nestick backend on Render

This folder deploys the **scraping engine + JSON API**. The browser UI is
served separately by Vercel (see `../vercel`).

## Deploy

1. Push this repo to GitHub.
2. In Render, **New → Blueprint**, connect the repo. Render finds
   `deploy/render/render.yaml`.
   Or from the CLI: `render blueprint deploy deploy/render/render.yaml`.
3. Set the optional paid API keys (`SERPAPI_KEY`, `HUNTER_API_KEY`,
   `GOOGLE_MAPS_KEY`, `NUMVERIFY_KEY`) in the service's Environment tab.
4. **To enable login:** add `AUTH_MONGODB_URI` and `JWT_SECRET` (see below).
5. Copy the service URL — Vercel needs it as `NESTICK_API_BASE`.

## Authentication (central auth database)

Optional and opt-in. Set **both** in the Render dashboard:

- `AUTH_MONGODB_URI` — the shared MongoDB from `CENTRAL_AUTH_GUIDE.md`
  (falls back to `MONGODB_URI` if unset).
- `JWT_SECRET` — the shared secret your Node platforms use.

When both are set, every `/api/*` route (except `/api/login` and `/healthz`)
requires `Authorization: Bearer <jwt>`, and the UI shows a sign-in page.
Passwords are verified against the central DB with bcrypt (the standard
Mongoose/bcryptjs pattern); tokens are HS256 JWTs signed with `JWT_SECRET`, so
tokens minted by your other platforms work here and vice-versa. Without the
env vars the app runs open, exactly as before.

## Verify

- `GET /healthz` → `{"status":"ok"}`
- `GET /api/status` → the live job state (empty until you start a run)
- Opening `https://<app>.onrender.com` shows the panel on the same origin.

## Security notes

- The CSRF/DNS-rebinding guard allows Host/Origin of `.onrender.com` and
  `.vercel.app`. Anyone with your service URL can start a scrape — the guard
  is **not** authentication. Add a token check before exposing this publicly.
- API keys are stored under `NESTICK_CONFIG_DIR` (`/var/data/nestick`) on the
  persistent disk so they survive redeploys.
