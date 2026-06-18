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

# Concurrent scans. Keep at 1 on the 2-OCPU / 12 GB A1: one scan gets both cores
# (fastest completion) and the box can never be pushed into swap by parallel big
# uploads. Raise only on a bigger host with RAM headroom (~4 GB resident per scan).
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
`N × cpu_count` processes fighting over the 2 cores. Run exactly one uvicorn process
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

## Landing the A1 (two stages: grab 1 OCPU, then resize to 2 OCPU on the box)

The Always Free **Ampere A1** allowance (2 OCPU / 12 GB ARM — this is the box
that runs the backend) is usually out of capacity, so grabbing it outright means
polling for hours/days. Two stages get you there without tying up your home PC
and without a throwaway micro box:

1. **Grab a 1-OCPU / 6 GB A1 from your PC** (`grab-a1.ps1`). A smaller slice
   lands far more often than the full 2 OCPU on contended free capacity, and
   6 GB already runs the backend. This is your always-on box.
2. **Resize it to 2 OCPU / 12 GB from the box itself** (`grab-a1.sh` under
   `grab-a1.service`). It can't *launch* a second 2-OCPU box — the tenancy cap is
   2 OCPU total, so 1 + 2 > 2 → `LimitExceeded`. Instead it resizes this instance
   up *in place*, retrying 24/7 until capacity frees up.

   **Why retrying can't lose you the box:** a flex OCPU change applies via an
   automatic reboot, and OCI validates capacity up front — if 2 OCPU isn't free
   the resize call is rejected immediately and the box keeps running, untouched,
   at 1 OCPU. Only a *successful* resize reboots it (briefly) into 2 OCPU. (Edge
   case: if a resize ever left the box stopped, the on-box script can't restart
   it — start it once from the console and it resumes from there.)

Files:

- `grab-a1.ps1` — Windows, Stage 1 (run from your PC to land the 1-OCPU box).
- `grab-a1.sh` + `grab-a1.service` — Linux, Stage 2 (runs on the A1, resizes up).

On the A1 box, after Stage 1 (one-time setup):

```bash
# 1. install the OCI CLI, then configure an api_key profile named API_KEY
#    (session auth needs a browser, so it can't work headless — api_key is required)
oci setup config            # create ~/.oci/config, profile name: API_KEY
#    ...and add an IAM policy allowing this user to manage the instance.
# 2. clone the repo, then run the upgrader under systemd so it survives reboots
sudo cp deploy/grab-a1.service /etc/systemd/system/
#    edit ExecStart path/user in the unit; optionally set NTFY_TOPIC for a phone push
sudo systemctl daemon-reload
sudo systemctl enable --now grab-a1
journalctl -u grab-a1 -f    # watch the attempts; "RESIZE ACCEPTED" when 2 OCPU lands
```

When the resize is accepted the script writes `~/oci-a1-SUCCESS.json` and the box
reboots into 2 OCPU; the unit re-runs on boot, sees it's already at 2 OCPU, and
exits cleanly (idempotent). It treats success / limit-hit / auth-fail as "done"
and won't restart on those, but self-heals across network blips and reboots.
Deploy the backend on this box and start `keepalive.py` on it (below) — both
stages aside, the box serves traffic the whole time.

## Keeping the A1 alive (idle-reclamation guard)

Oracle may **reclaim** an Always Free instance that stays idle for a rolling
7-day window — but only if 95th-percentile CPU **and** network **and** memory
utilization are *all* under ~20% the whole time. It's an AND, so holding any one
metric above the line is enough. `keepalive.py` holds the cheapest one: it parks
3 GiB of RAM resident (~30% of the 12 GB A1) at ~0% CPU, instead of burning the
free box with a fake-traffic or CPU-spin loop. This is a slow 7-day reclaim, not
a per-request spin-down — once running, the box answers every request instantly.

```bash
sudo cp deploy/keepalive.service /etc/systemd/system/
# edit ExecStart path in the unit if the repo isn't at /opt/portal-mapper
sudo systemctl daemon-reload
sudo systemctl enable --now keepalive
journalctl -u keepalive -f          # confirm "resident: … pages held"
```

Tunables (env, set in the unit): `KEEPALIVE_MB` (default 3072 — the held MiB;
3 GiB clears the 2.4 GB / 20% line with margin and still leaves ~8.5 GB for a
scan) and `KEEPALIVE_INTERVAL_SEC` (default 300 — re-touch cadence). The unit
sets `OOMScoreAdjust=900` so if a real scan ever needs the RAM, the kernel kills
the keepalive first and systemd restarts it afterward — the backend is never the
casualty.

## Host / network checklist

- Oracle security list / firewall: ingress only on **443** (and **80** for the
  ACME challenge + redirect). Restrict **22** (SSH) to your own IP.
- TLS: `certbot --nginx` (Let's Encrypt). Replace `YOUR_DOMAIN` in `nginx.conf`.
- Disk: uploads spool to the OS temp dir; with the 1-at-a-time gate, peak temp
  usage is ~4 GB. Make sure `/tmp` (or `TMPDIR`) has the headroom.
- Watch for Oracle A1 "out of capacity" on launch — retry or change region.
