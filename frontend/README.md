# Portal Mapper — Frontend

React 19 + TypeScript + Vite frontend for uploading Minecraft save files and
visualizing block locations on an interactive map.

## Requirements

- Node.js 20+ (required by Vite 8)

## Setup

```bash
npm install
```

## Running

```bash
npm run dev
```

The app will be available at `http://localhost:5173`.

The dev server expects the backend running at `http://localhost:8000`. See the [backend README](../backend/README.md) for setup.

### Pointing at a different backend

The API URL is read from the `VITE_API_URL` env var at build/dev time, falling
back to `http://localhost:8000/parse-blocks`. For a deployed backend, set it
before building:

```bash
VITE_API_URL=https://your-host/parse-blocks npm run build
```

## Other Commands

```bash
npm run build    # production build → dist/
npm run preview  # preview the production build locally
npm run lint     # run ESLint
```

## Deploying to GitHub Pages

The frontend is a static bundle, so it's hosted on GitHub Pages and redeployed
automatically. The workflow is
[`.github/workflows/deploy-frontend.yml`](../.github/workflows/deploy-frontend.yml):
on every push to `master` that touches `frontend/**`, it runs `npm ci` +
`npm run build` and publishes `frontend/dist`. Live URL:
`https://sambrothers0.github.io/portal-mapper/`. (Trigger it manually any time
from the Actions tab via **Run workflow**.)

**One-time setup (in the GitHub repo):**

1. **Settings → Pages → Build and deployment → Source: "GitHub Actions".**
   (Not the older "Deploy from a branch" mode.)
2. **Settings → Secrets and variables → Actions → Variables →** add a repository
   *variable* (not a secret) named `VITE_API_URL` pointing at the deployed
   backend, e.g. `https://your-backend-host/parse-blocks`. If it's left unset the
   build still succeeds but the bundle falls back to
   `http://localhost:8000/parse-blocks`, so the live site can't reach a backend
   until this is set and the workflow re-runs.

**Two build-time details that make Pages work:**

- **Base path.** A project site is served from a subpath
  (`/portal-mapper/`), so all asset URLs must be prefixed with it. The workflow
  sets `VITE_BASE=/portal-mapper/`, which `vite.config.ts` reads into Vite's
  `base`. Local dev and a plain `npm run build` default to `/`, so nothing
  changes locally. **If the repo is ever renamed, update `VITE_BASE` in the
  workflow to match** (or it 404s on assets).
- **Backend URL is compiled in.** `VITE_API_URL` is read at *build* time, so
  changing the backend address means re-running the workflow, not just editing a
  setting. The backend must also (a) allow this Pages origin via
  `ALLOWED_ORIGINS`, and (b) be served over **HTTPS** — Pages is HTTPS-only, and
  a browser blocks an HTTPS page from calling an HTTP API (mixed content).

## How it works

1. **Pick a save + dimension.** The selected `.zip` is validated against the
   4 GB limit on both select and submit.
2. **Filter in the browser** (`src/workers/regionFilter.worker.ts`). Before any
   upload, a web worker pares the save down to just the chosen dimension's
   `region/*.mca` (+ `.mcc`) files, repacked with the original paths preserved.
   This commonly shrinks a multi-GB upload by an order of magnitude. The blob is
   passed by reference so a 4 GB save is never read into a main-thread buffer.
3. **Upload + scan.** The filtered zip is POSTed to the backend with a real
   byte-level progress bar; once it lands, the backend scans and returns matches.
4. **Map** (`src/WorldMap.tsx`). Matches are plotted on a single canvas on
   Minecraft's X/Z plane — chunk-aligned grid, drag-to-pan, wheel/pinch zoom,
   auto-fit, hover-for-coordinates.

The UI runs through four phases — `idle → filtering → uploading → processing` —
reflected in both the on-page indicators and the browser tab title.

> **Important:** the worker's `regionDimension()` is an exact mirror of the
> backend's `_region_dimension()`. If one changes, change the other — the
> backend stays the source of truth for how a path maps to a dimension.

## Notable toolchain

Uses the React Compiler (`babel-plugin-react-compiler`) with strict
`eslint-plugin-react-hooks` v7. Practical consequence in `WorldMap.tsx`: **no
`setState` inside effect bodies** — keep state updates in event / `requestAnimationFrame`
/ `ResizeObserver` callbacks, and read live view/hover state from refs during
the canvas draw.