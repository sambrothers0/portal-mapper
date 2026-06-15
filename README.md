# Portal Mapper

A web app for finding and visualizing every block of a given type in a Minecraft
world. Upload a zipped save, pick a dimension and a block type (defaults to
`minecraft:nether_portal`), and the matching coordinates are plotted on an
interactive map.

Built to handle large saves — up to 4 GB — as fast as reasonably possible.

This is the single source of documentation for the project, including guidance
for AI agents (see [Architecture & invariants](#architecture--invariants)). Keep
it current when the architecture or its invariants change.

## Repository layout

| Path | What it is |
|---|---|
| `backend/` | FastAPI service that parses the save and returns match coordinates. Pure-stdlib NBT scanner, parallel across CPU cores. |
| `frontend/` | React 19 + TypeScript + Vite UI. Filters the save to one dimension *in the browser* before upload, then renders the results on a canvas map. |
| `testing/` | Sample save zips used for manual verification (`test1.zip` ~1.4 MB, `test2.zip` ~1.18 GB). |

There is no root build tool; the two halves are developed and run independently.

## Quick start

Two terminals, backend first:

```bash
# backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload          # http://localhost:8000
```

```bash
# frontend
cd frontend
npm install
npm run dev                        # http://localhost:5173
```

The frontend talks to the backend at `http://localhost:8000` by default; override
with `VITE_API_URL` (see [Frontend](#frontend)).

## How it fits together

1. The browser strips the uploaded save to just the chosen dimension's
   `region/*.mca` files (a web worker), so a multi-GB world usually uploads as a
   fraction of its size.
2. The backend reads those region files straight out of the zip (no temp files),
   fans the per-file parsing across a `ProcessPoolExecutor`, and decodes chunks
   with the in-repo `fastnbt.py` selective NBT scanner.
3. The matched coordinates come back as `{ block_type, dimension, count, matches }`
   and are drawn on the map.

The UI runs through four phases — `idle → filtering → uploading → processing` —
reflected in both the on-page indicators and the browser tab title.

---

## Backend

FastAPI service that accepts a zipped Minecraft save folder and returns the
coordinates of every block matching a given type (defaults to
`minecraft:nether_portal`).

It's built to handle large saves: the upload is read straight from disk (the
zip — up to 4 GB — is never held in memory), only the `region/*.mca` files are
read out of it, and those region files are parsed in parallel across every CPU
core. NBT decoding is done by an in-repo, pure-stdlib scanner (`fastnbt.py`) —
no native NBT library, so nothing needs compiling on the target (ARM) host.

### Requirements

- Python 3.10+ (3.13 recommended; that's what the production image uses)

### Setup

```bash
pip install -r requirements.txt
```

Dependencies: `fastapi`, `uvicorn[standard]`, and `python-multipart` (multipart
upload parsing). Region/chunk decoding is handled by the bundled `fastnbt.py`,
which is pure standard library — there is no third-party NBT dependency.

### Running

Development (auto-reload):

```bash
uvicorn main:app --reload
```

Production — run **without** `--reload`:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API is available at `http://localhost:8000`. CORS defaults to open (`*`) for
local dev; set `ALLOWED_ORIGINS` to the frontend's origin in production. When
running behind a reverse proxy, raise the proxy's body-size limit and read
timeout to allow multi-gigabyte uploads. See `deploy/` for a hardened nginx
config and the full public-deployment checklist.

**Configuration (env vars):**

| Var | Default | Purpose |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` | Comma-separated origins allowed to call the API. |
| `MAX_CONCURRENT_SCANS` | `1` | How many scans may run at once. One scan gets every core (fastest completion) and bounds memory; raise only with RAM headroom. Extra requests get `503`. |
| `FULL_ACCESS_KEYS` | *(empty)* | Comma-separated access codes that raise the upload limit from 250 MB to 4 GB. Sent by the client in the `X-Access-Key` header. Empty = nobody has full access. |

### How it works

1. The multipart upload is streamed to Starlette's spooled temp file, so a 4 GB
   archive never lands in RAM.
2. Only `.mca`/`.mcc` members living directly inside a `region/` directory are
   read out of the zip — straight into memory, no temp directory (Overworld
   `region`, Nether `DIM-1/region`, End `DIM1/region`). Everything else —
   `entities`, `poi`, `playerdata`, `level.dat`, … — is skipped, since it's
   irrelevant to a block search and can be large.
3. Each region file is an independent unit of work, fanned out across a
   `ProcessPoolExecutor` (sized to `os.cpu_count()`) to sidestep the GIL on the
   CPU-bound parsing. The pool is created once for the life of the process.
4. Within each chunk, `fastnbt.py` walks the NBT byte stream selectively: it
   reads each section's block palette first and only decodes the packed block
   array when the target block actually appears in that palette — cheap for
   sparse targets like portals.
5. Results from every region file are merged and returned.

There is no incremental progress reporting — the endpoint returns a single
response once the whole scan completes.

### Endpoints

#### `POST /parse-blocks`

Accepts a multipart form upload.

| Field | Type | Description |
|---|---|---|
| `file` | `.zip` file | Zipped Minecraft save folder. Upload limit is 250 MB, or 4 GB with a valid access code (see header below). |
| `block_type` | string (optional) | Block type to search for (default: `minecraft:nether_portal`). A value without a namespace, e.g. `stone`, is normalized to `minecraft:stone`. |
| `dimension` | string (optional) | Which dimension to scan: `overworld` (default), `nether`, or `end`. Only that dimension's region files are extracted and parsed. |

Optional header `X-Access-Key`: one of the server's `FULL_ACCESS_KEYS`. A valid
code raises the upload limit from 250 MB to 4 GB.

The dimension is resolved from the save's directory layout: the Overworld's
`region/` sits in the world root, the Nether's under `DIM-1/`, and the End's
under `DIM1/`.

**Example with curl:**

```bash
curl -X POST http://localhost:8000/parse-blocks \
  -F "file=@my_world.zip" \
  -F "block_type=minecraft:nether_portal" \
  -F "dimension=nether"
```

**Response `200`:**

```json
{
  "block_type": "minecraft:nether_portal",
  "dimension": "nether",
  "count": 3,
  "truncated": false,
  "matches": [
    { "x": 128, "y": 64, "z": -512 }
  ]
}
```

Coordinates are absolute world coordinates. `count` is the true total found;
`matches` is capped at 100,000 entries, with `truncated: true` when the cap was
hit.

**Error responses:**

| Status | When |
|---|---|
| `400` | Upload is missing, not a `.zip`, not a valid zip archive, or `dimension` is not one of `overworld`/`nether`/`end` |
| `413` | Upload exceeds the tier limit — 250 MB, or 4 GB with a valid `X-Access-Key` (checked via `Content-Length`, before the body is read) |
| `422` | No region files found for the requested dimension (not a valid save, or that dimension was never visited) |
| `503` | Server is already running a scan (`MAX_CONCURRENT_SCANS` reached); includes a `Retry-After` header — retry shortly |

Each error body is `{ "detail": "<message>" }`.

---

## Frontend

React 19 + TypeScript + Vite frontend for uploading Minecraft save files and
visualizing block locations on an interactive map.

### Requirements

- Node.js 20+ (required by Vite 8)

### Setup

```bash
npm install
```

### Running

```bash
npm run dev
```

The app will be available at `http://localhost:5173`.

The dev server expects the backend running at `http://localhost:8000`.

#### Pointing at a different backend

The API URL is read from the `VITE_API_URL` env var at build/dev time, falling
back to `http://localhost:8000/parse-blocks`. For a deployed backend, set it
before building:

```bash
VITE_API_URL=https://your-host/parse-blocks npm run build
```

### Other commands

```bash
npm run build    # production build → dist/
npm run preview  # preview the production build locally
npm run lint     # run ESLint
```

### Deployment (GitHub Pages)

The frontend is a static bundle deployed to GitHub Pages by
[`.github/workflows/deploy-frontend.yml`](.github/workflows/deploy-frontend.yml):
every push to `master` touching `frontend/**` rebuilds and publishes
`frontend/dist` to `https://sambrothers0.github.io/portal-mapper/`. One-time
setup (Pages source = "GitHub Actions"; an Actions variable `VITE_API_URL`
pointing at the deployed backend) and the base-path / CORS / HTTPS details are in
the [frontend README](frontend/README.md#deploying-to-github-pages).

### How it works

1. **Pick a save + dimension.** The selected `.zip` is validated against the
   hard 4 GB ceiling on both select and submit. An optional access code (with a
   show/hide toggle, remembered in `localStorage`) raises the upload limit.
2. **Filter in the browser** (`src/workers/regionFilter.worker.ts`). Before any
   upload, a web worker pares the save down to just the chosen dimension's
   `region/*.mca` (+ `.mcc`) files, repacked with the original paths preserved.
   This commonly shrinks a multi-GB upload by an order of magnitude. The blob is
   passed by reference so a 4 GB save is never read into a main-thread buffer.
   The *filtered* result is then checked against the upload limit (250 MB, or
   4 GB with a valid access code) — the same number the backend enforces.
3. **Upload + scan.** The filtered zip is POSTed to the backend (with the
   `X-Access-Key` header when a code is set) and a real byte-level progress bar;
   once it lands, the backend scans and returns matches.
4. **Map** (`src/WorldMap.tsx`). Matches are plotted on a single canvas on
   Minecraft's X/Z plane — chunk-aligned grid, drag-to-pan, wheel/pinch zoom,
   auto-fit, hover-for-coordinates.

---

## Architecture & invariants

Guidance for contributors and AI agents working in this repo.

### Running & checks

| | Command (from the subdir) |
|---|---|
| Backend dev | `uvicorn main:app --reload` |
| Backend deps | `pip install -r requirements.txt` |
| Frontend dev | `npm run dev` |
| Frontend typecheck | `npx tsc --noEmit` |
| Frontend lint | `npm run lint` |
| Frontend build | `npm run build` (runs `tsc -b` then `vite build`) |

Always run `tsc`/`lint` after touching frontend code — the lint config is strict
(see "React Compiler" below). There is no automated test suite; verification is
done by running the real endpoint against the `testing/` zips.

### Architecture map

**Backend (`backend/`)**
- `main.py` — FastAPI app. `POST /parse-blocks` (multipart: `file`, `block_type`,
  `dimension`). Reads only the requested dimension's region members out of the
  spooled upload (no temp dir), then fans `scan_region_bytes` across a
  `ProcessPoolExecutor` created in the lifespan handler. Tiered `Content-Length`
  guard (250 MB / 4 GB by `X-Access-Key`) in the `enforce_upload_limit`
  middleware; a `MAX_CONCURRENT_SCANS` semaphore serializes scans (`503` when
  busy); CORS origins from `ALLOWED_ORIGINS`. Deploy hardening in `deploy/`.
- `parser.py` — region-file (`.mca`) sector-table walk and chunk decompression.
  `_walk_region(data, read_external)` is the shared core; `scan_region_bytes`
  (zip/in-memory path, the parallel work unit) and `scan_region_file` (disk path,
  kept for benchmarks) both delegate to it.
- `fastnbt.py` — hand-written, pure-stdlib selective NBT scanner. `scan_chunk`
  reads each section's palette first and only decodes the packed block array when
  the target block is present. **Deliberately no third-party NBT library** so
  nothing needs compiling on the ARM deploy host. Don't reintroduce one.

**Frontend (`frontend/src/`)**
- `App.tsx` — top-level flow and the four-phase state machine
  (`idle → filtering → uploading → processing`), driven from `handleSubmit`.
  Owns the progress UI, phase-based tab title, and result display.
- `workers/regionFilter.worker.ts` — web worker that pares the save to one
  dimension's region files before upload (see invariant below).
- `WorldMap.tsx` — single-canvas map (X/Z plane). Pan/zoom/auto-fit/hover, all
  drawn imperatively from refs.
- `PortalLoader.tsx`, `index.css` (Tailwind v4 + ambient background animation).

### Cross-cutting invariants (don't break these)

1. **Dimension-from-path must match on both sides.** The worker's
   `regionDimension()` (`regionFilter.worker.ts`) is an exact mirror of the
   backend's `_region_dimension()` (`main.py`): a member is a region file only if
   its path is `…/region/<name>.mca|.mcc`, and the dimension comes from the parent
   dir (`DIM-1`→nether, `DIM1`→end, else overworld). The worker also preserves
   original arcnames when repacking so the backend re-derives the same dimension.
   Change one, change the other; the backend is the source of truth.
2. **Upload limit is tiered (250 MB / 4 GB by access code) and checked on the
   *uploaded* zip.** Backend `enforce_upload_limit` middleware gates on
   `Content-Length` by `X-Access-Key`; the frontend mirrors the same numbers on
   the *filtered* blob and a hard 4 GB ceiling on the original at select time.
   Keep the frontend tiers in sync with the backend.
3. **Never load the whole save into memory.** Frontend passes the `File`/Blob by
   reference into the worker; backend reads from Starlette's on-disk spooled file.
4. **React Compiler is on.** `babel-plugin-react-compiler` + strict
   `eslint-plugin-react-hooks` v7. Practical rules, mainly in `WorldMap.tsx`:
   - **No `setState` in an effect body.** Put state updates in event /
     `requestAnimationFrame` / `ResizeObserver` callbacks instead.
   - Read live view/hover state from refs inside the canvas `draw`.
   - Lazy ref init via `if (ref.current == null)`.
   - When cancelling an animation frame on cleanup, **null the ref afterwards** —
     a stale non-null `rafRef` wedges `scheduleDraw` across a StrictMode remount.
5. **Response shape** is `{ block_type, dimension, count, truncated, matches }`
   where `matches` is `[{ x, y, z }]` (absolute world coords), capped at 100,000
   with `truncated` flagging the cap. The map consumes it directly.

### Known risks / TODO

- **Untrusted-upload hardening (handled).** A client can POST any zip directly
  (the browser filter isn't a security boundary), so the backend self-defends:
  zip-bomb guard (~4 GB total decompressed-bytes cap → `413`), 64 MB per-chunk
  decompress cap, matches counted but stored only up to 100,000 (`truncated`
  flag), and per-chunk parse errors skipped instead of crashing. The capped set
  can still be a heavy canvas/hover-scan; a more compact match serialization is a
  possible follow-up.
- **Deployment is the remaining work:** the `Dockerfile` (`python:3.13-slim`,
  arm64) is still TODO; the nginx reverse-proxy config and the public-deployment
  checklist now live in `deploy/`. Run uvicorn without `--reload` and as a single
  process. Target is Oracle Cloud Always Free, Ampere A1 (arm64).
- `GZipMiddleware` for large JSON responses is enabled. Optional remaining perf
  items (non-blocking): a more compact match serialization; read+scan pipelining.

## Status

Single-user personal tool, being prepared for limited public use. The app itself
is feature-complete and hardened for public exposure (tiered upload limit with
access codes, scan-concurrency gate, configurable CORS, per-IP rate limiting in
the nginx config). The frontend auto-deploys to GitHub Pages (see
[Deployment](#deployment-github-pages)); the backend `Dockerfile` for the Oracle
A1 host is the remaining deployment work.
