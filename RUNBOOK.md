# LightChat — one-page runbook (lightchain.ai eng)

**Updated:** 2026-08-18 · KeikoDev  
**Goal:** Thin API host + static PWA. Chat bodies prefer P2P; host = auth, signaling, roles, history fallback, media URLs.

---

## What you run

| Piece | Today | Target |
|-------|--------|--------|
| Static PWA | `index.html` + JS on CDN / Pages / Caddy | `chat.lightchain.ai` or keep `lightchat.chat` |
| API | Flask + Socket.IO (`server.py`) | Your container platform |
| Data | SQLite volume at `DATA_DIR` | Same volume or Postgres later |
| Media | `MEDIA_BACKEND=fs` or `s3` (R2) | **Not** new blobs in SQLite |
| Optional | Holepunch DHT relay (Node) | Separate small service |

```bash
cp env.example .env          # fill secrets
docker compose up -d --build # API on :8080
curl -s localhost:8080/health | jq .
```

Static: open `index.html` with `BACKEND` pointed at the API (or `docker compose --profile with-web up`).

---

## Env (required / important)

| Var | Purpose |
|-----|---------|
| `SECRET_KEY` | Flask secret |
| `DATA_DIR` | Persist SQLite + fs media (default `/app/data`) |
| `MEDIA_BACKEND` | `fs` (recommended bridge) · `s3` (R2) · `local` (legacy SQLite) |
| `LIGHTCHAT_OWNER_WALLETS` | `0x…,0x…` max 2 dual owners |
| `MEDIA_S3_*` | Only if `MEDIA_BACKEND=s3` — see `env.example` |
| `METERED_API_KEY` | Optional TURN for WebRTC NAT |
| `LIGHTCHAT_BOT_TOKEN` | Optional garden/webhook bots |
| `PORT` | Default `8080` |

Frontend: set `BACKEND` in `index.html` (or inject at build) to the public API HTTPS URL.

---

## Health

`GET /health` returns:

- `status`, `auth`
- `db.path` / `db.bytes` / `db.garden_waters`
- `media.backend` / `media.ready`

If `media.ready` is false with `s3`, fix env / install boto3.

---

## Owners & ops

1. Set `LIGHTCHAT_OWNER_WALLETS` before first traffic (or first joiner becomes owner).  
2. Staff: mods channel, timeouts, bans, announcements.  
3. `#tree` (garden) uses SQLite + event log — volume must persist.  
4. Log off / wallet auth: Lightchain chain **9200** only.

---

## Media policy

- Prefer **YouTube** for long video.  
- Caps: ~8 MB image / ~20 MB video.  
- New uploads with `fs` → `/media/...` on the volume.  
- New uploads with `s3` → `MEDIA_S3_PUBLIC_BASE/...`.  
- Legacy `/chat-image/id` and `/chat-file/id` still served from SQLite.

---

## Smoke / soak

- See `SMOKE.md` (2-wallet checklist).  
- See `SOAK.md` (3+ wallet Discord-replace soak).  
Do not call handoff “done” until smoke passes on staging; soak before claiming day-to-day Discord replace.

---

## Not day-1 requirements

Full Holepunch default · zero relay · Postgres · pixel-perfect Discord · migrating old SQLite blobs.

---

## Contacts

Public: **KeikoDev** · product free for everyone · no personal email in ops docs.
