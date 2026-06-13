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

The API is available at `http://localhost:8000`. CORS is currently open to all
origins. When running behind a reverse proxy, raise the proxy's body-size limit
and read timeout to allow multi-gigabyte uploads.

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
| `file` | `.zip` file | Zipped Minecraft save folder (max 4 GB) |
| `block_type` | string (optional) | Block type to search for (default: `minecraft:nether_portal`). A value without a namespace, e.g. `stone`, is normalized to `minecraft:stone`. |
| `dimension` | string (optional) | Which dimension to scan: `overworld` (default), `nether`, or `end`. Only that dimension's region files are extracted and parsed. |

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
  "matches": [
    { "x": 128, "y": 64, "z": -512 }
  ]
}
```

Coordinates are absolute world coordinates.

**Error responses:**

| Status | When |
|---|---|
| `400` | Upload is missing, not a `.zip`, not a valid zip archive, or `dimension` is not one of `overworld`/`nether`/`end` |
| `413` | Upload exceeds the 4 GB limit (checked via `Content-Length`) |
| `422` | No region files found for the requested dimension (not a valid save, or that dimension was never visited) |

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

### How it works

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
  `ProcessPoolExecutor` created in the lifespan handler. Server-side 4 GB
  `Content-Length` guard. CORS open to all origins.
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
2. **4 GB limit is enforced in three places** — frontend select, frontend submit,
   backend `Content-Length`. Keep them consistent.
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
5. **Response shape** is `{ block_type, dimension, count, matches }` where
   `matches` is `[{ x, y, z }]` (absolute world coords). The map consumes it
   directly.

### Known risks / TODO

- **No cap on matches.** A common block (e.g. `stone`) can return millions of
  coordinates → multi-GB JSON and a canvas/hover-scan that can hang the browser.
  A result cap + "too many results" message is recommended before exposing
  arbitrary block types broadly.
- **Deployment is the remaining work:** Dockerfile (`python:3.13-slim`, arm64) +
  nginx reverse proxy (`client_max_body_size 4500M`, raised `proxy_read_timeout`,
  `proxy_request_buffering off`); run uvicorn without `--reload`. Target is Oracle
  Cloud Always Free, Ampere A1 (arm64).
- Optional backend perf items (non-blocking): `GZipMiddleware` for large JSON
  responses; a more compact match serialization; read+scan pipelining.

## Status

Single-user personal tool. The app itself is feature-complete; deployment is the
remaining work. The 4 GB upload ceiling is enforced on both client and server.
