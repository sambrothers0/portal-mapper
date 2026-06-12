# Portal Mapper — Backend

FastAPI service that accepts a zipped Minecraft save folder and returns the coordinates of blocks matching a given type (defaults to `minecraft:nether_portal`).

## Requirements

- Python 3.10+

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`.

## Endpoints

### `POST /parse-blocks`

Accepts a multipart form upload.

| Field | Type | Description |
|---|---|---|
| `file` | `.zip` file | Zipped Minecraft save folder |
| `block_type` | string (optional) | Block type to search for (default: `minecraft:nether_portal`) |

**Example with curl:**

```bash
curl -X POST http://localhost:8000/parse-blocks \
  -F "file=@my_world.zip" \
  -F "block_type=minecraft:nether_portal"
```

**Response:**

```json
{
  "block_type": "minecraft:nether_portal",
  "count": 3,
  "matches": [...]
}
```
