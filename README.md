# Portal Mapper — Backend

FastAPI service that accepts a zipped Minecraft save folder and returns the
coordinates of every block matching a given type (defaults to
`minecraft:nether_portal`).

It's built to handle large saves: the upload is read straight from disk (the
zip — up to 4 GB — is never held in memory), only the `region/*.mca` files are
extracted, and those region files are parsed in parallel across every CPU core.

## Requirements

- Python 3.10+ (3.13 recommended; that's what the production image uses)

## Setup

```bash
pip install -r requirements.txt
```

Dependencies: `fastapi`, `uvicorn[standard]`, `NBT` (region/chunk decoding), and
`python-multipart` (multipart upload parsing).

## Running

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

## How it works

1. The multipart upload is streamed to Starlette's spooled temp file, so a 4 GB
   archive never lands in RAM.
2. Only `.mca`/`.mcc` members living directly inside a `region/` directory are
   extracted (Overworld `region`, Nether `DIM-1/region`, End `DIM1/region`).
   Everything else — `entities`, `poi`, `playerdata`, `level.dat`, … — is
   skipped, since it's irrelevant to a block search and can be large.
3. Each region file is an independent unit of work, fanned out across a
   `ProcessPoolExecutor` (sized to `os.cpu_count()`) to sidestep the GIL on the
   CPU-bound parsing. The pool is created once for the life of the process.
4. Results from every region file are merged and returned.

There is no incremental progress reporting — the endpoint returns a single
response once the whole scan completes.

## Endpoints

### `POST /parse-blocks`

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
