# CLAUDE.md

Guidance for AI agents working in this repo. Keep this file current when the
architecture or its invariants change.

## What this is

A two-part web app that finds every block of a given type in a Minecraft save
and plots the coordinates on a map. Single-user personal tool, built to handle
saves up to **4 GB**. See `README.md` for the product overview.

- `backend/` — FastAPI service (Python 3.10+, 3.13 in prod).
- `frontend/` — React 19 + TypeScript + Vite (Node 20+).
- `testing/` — sample save zips for manual checks (`test1.zip` ~1.4 MB,
  `test2.zip` ~1.18 GB).

There is no root build tool; the two halves are developed and run independently.

## Running & checks

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

## Architecture map

### Backend (`backend/`)
- `main.py` — FastAPI app. `POST /parse-blocks` (multipart: `file`, `block_type`,
  `dimension`). Reads only the requested dimension's region members out of the
  spooled upload (no temp dir), then fans `scan_region_bytes` across a
  `ProcessPoolExecutor` created in the lifespan handler. Server-side 4 GB
  `Content-Length` guard. A scan-concurrency semaphore (`MAX_CONCURRENT_SCANS`,
  default 1, also made in lifespan) serializes heavy scans and returns `503 +
  Retry-After` when busy, so concurrent uploads can't OOM the box. CORS origins
  come from `ALLOWED_ORIGINS` (default `*` for dev; set to the frontend origin in
  prod). Public deploy hardening (per-IP rate/conn limits, body streaming, TLS,
  single-uvicorn-process rule) lives in `deploy/` (`nginx.conf` + `README.md`).
- `parser.py` — region-file (`.mca`) sector-table walk and chunk decompression.
  `_walk_region(data, read_external)` is the shared core; `scan_region_bytes`
  (zip/in-memory path, the parallel work unit) and `scan_region_file` (disk path,
  kept for benchmarks) both delegate to it.
- `fastnbt.py` — hand-written, pure-stdlib selective NBT scanner. `scan_chunk`
  reads each section's palette first and only decodes the packed block array when
  the target block is present. **Deliberately no third-party NBT library** so
  nothing needs compiling on the ARM deploy host. Don't reintroduce one.

### Frontend (`frontend/src/`)
- `App.tsx` — top-level flow and the four-phase state machine
  (`idle → filtering → uploading → processing`), driven from `handleSubmit`.
  Owns the progress UI, phase-based tab title, and result display.
- `workers/regionFilter.worker.ts` — web worker that pares the save to one
  dimension's region files before upload (see invariant below).
- `WorldMap.tsx` — single-canvas map (X/Z plane). Pan/zoom/auto-fit/hover, all
  drawn imperatively from refs.
- `PortalLoader.tsx`, `index.css` (Tailwind v4 + ambient background animation).

## Cross-cutting invariants (don't break these)

1. **Dimension-from-path must match on both sides.** The worker's
   `regionDimension()` (`regionFilter.worker.ts`) is an exact mirror of the
   backend's `_region_dimension()` (`main.py`): a member is a region file only if
   its path is `…/region/<name>.mca|.mcc`, and the dimension comes from the parent
   dir (`DIM-1`→nether, `DIM1`→end, else overworld). The worker also preserves
   original arcnames when repacking so the backend re-derives the same dimension.
   Change one, change the other; the backend is the source of truth.
2. **Upload limit is tiered and enforced on the *uploaded* (post-filter) zip.**
   Visitors get `PUBLIC_MAX_UPLOAD_BYTES` (250 MB); a valid `X-Access-Key`
   (one of `FULL_ACCESS_KEYS`, env) raises it to `FULL_MAX_UPLOAD_BYTES` (4 GB).
   Enforced in the backend `enforce_upload_limit` middleware (on `Content-Length`,
   before the body is parsed) and mirrored in the frontend, which checks the
   *filtered* blob against the same numbers and sends the code header. The
   frontend's *select*-time check is a separate hard 4 GB ceiling on the original
   file. Keep the frontend tier numbers (`App.tsx`) in sync with the backend.
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

## Known risks / TODO

- **Untrusted-upload hardening (done).** A client can POST any zip to
  `/parse-blocks` (the browser filter isn't a security boundary), so the backend
  defends itself: members are read through a ~4 GB total decompressed-bytes cap
  (`MAX_DECOMPRESSED_REGION_BYTES`, `main.py`) → `413` on a zip bomb; each chunk
  decompresses under a 64 MB hard cap (`parser.py`); matches are counted but only
  stored up to `MAX_MATCHES` via `fastnbt.MatchSink` (true `count` kept,
  `truncated` flagged); per-chunk parse errors are caught and the chunk skipped.
- **Deployment is the remaining work:** Dockerfile (`python:3.13-slim`, arm64) +
  nginx reverse proxy (`client_max_body_size 4500M`, raised `proxy_read_timeout`,
  `proxy_request_buffering off`); run uvicorn without `--reload`. Target is Oracle
  Cloud Always Free, Ampere A1 (arm64).
- Optional backend perf items (non-blocking): `GZipMiddleware` for large JSON
  responses; a more compact match serialization; read+scan pipelining.
