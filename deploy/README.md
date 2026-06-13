# Deploying portal-mapper for public use

The app is a single-user-scale personal tool exposed to the open internet. The
defences are layered: **nginx** throttles who gets in, the **FastAPI app**
bounds what actually runs, and the **host/network** config shrinks the attack
surface. None of these slow down a legitimate single scan — that request still
gets the whole process pool and runs at full speed.

## What protects the live site

| Layer | Control | Where |
|---|---|---|
| App | Serialize scans (1 at a time) so concurrent uploads can't OOM the box | `MAX_CONCURRENT_SCANS`, `main.py` |
| App | Fail fast with `503 + Retry-After` when a scan is already running | `main.py` `/parse-blocks` |
| App | CORS locked to the real frontend origin | `ALLOWED_ORIGINS`, `main.py` |
| App | Tiered upload limit: 250 MB for visitors, 4 GB with an access code → `413` | `FULL_ACCESS_KEYS`, `main.py` |
| App | Match cap (100k) → `truncated` flag | `main.py` (unchanged) |
| nginx | Per-IP request-rate limit + simultaneous-connection cap → `429` | `nginx.conf` |
| nginx | 4 GB body ceiling, streamed (not buffered) to the backend | `nginx.conf` |
| nginx | Long read/send timeouts so a real multi-minute scan isn't cut off | `nginx.conf` |
| Host | Only 80/443 open to the world; SSH restricted to your IP | Oracle security list |

## Backend env vars (set in prod)

```bash
# Lock CORS to the deployed frontend's exact origin (comma-separated for more).
ALLOWED_ORIGINS=https://your-frontend-domain

# Concurrent scans. Keep at 1 on the 4-core A1: one scan gets all cores (fastest
# completion) and the box can never be pushed into swap by parallel big uploads.
# Raise only on a bigger host with RAM headroom (~4 GB resident per scan).
MAX_CONCURRENT_SCANS=1

# Access codes that unlock the 4 GB upload limit (visitors are capped at 250 MB).
# Comma-separated — give each trusted person their own code so you can revoke one
# without disturbing the rest. Empty/unset = nobody has full access (even you), so
# set this before relying on your own code. It's a soft gate to raise the limit,
# not authentication: anyone holding a code can use or share it.
FULL_ACCESS_KEYS=your-private-code,a-code-for-a-friend
```

### Granting / revoking access

1. Pick codes (any hard-to-guess strings) and put them in `FULL_ACCESS_KEYS`.
2. Restart the backend so it picks up the new env.
3. Give a person their code; they paste it into the site's **Access code** field
   once (it's remembered in their browser) and can then upload up to 4 GB.
4. To revoke, delete that code from `FULL_ACCESS_KEYS` and restart — only that
   code stops working.

## Run the backend with ONE uvicorn process

The app shares a single `ProcessPoolExecutor` sized to `os.cpu_count()`. Running
uvicorn with `--workers N` would give each worker its **own** pool →
`N × cpu_count` processes fighting over 4 cores. Run exactly one uvicorn process
and let the pool provide the parallelism. No `--reload` in prod.

```bash
uvicorn main:app --host 127.0.0.1 --port 8000        # one process, behind nginx
```

Bind to `127.0.0.1`, not `0.0.0.0` — only nginx should reach it.

## Frontend hosting

The frontend is a static `dist/` build. Two options:

1. **Same Oracle box** — nginx serves `dist/` and proxies `/parse-blocks` (this
   config). One origin; you could even drop CORS.
2. **Static CDN (recommended for public use)** — host `dist/` on Cloudflare
   Pages / Netlify (free, global CDN, free TLS) and point it at the backend via
   `VITE_API_URL`. The Oracle box then only ever serves the one rate-limited,
   concurrency-gated endpoint. Set `ALLOWED_ORIGINS` to the Pages domain.

## Host / network checklist

- Oracle security list / firewall: ingress only on **443** (and **80** for the
  ACME challenge + redirect). Restrict **22** (SSH) to your own IP.
- TLS: `certbot --nginx` (Let's Encrypt). Replace `YOUR_DOMAIN` in `nginx.conf`.
- Disk: uploads spool to the OS temp dir; with the 1-at-a-time gate, peak temp
  usage is ~4 GB. Make sure `/tmp` (or `TMPDIR`) has the headroom.
- Watch for Oracle A1 "out of capacity" on launch — retry or change region.
