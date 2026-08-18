# LightChat — 2-wallet staging smoke

**Pass this before telling Jason hosting is ready.**  
**Updated:** 2026-08-18

Use two Lightchain wallets (chain **9200**). Hard-refresh the PWA first.

| # | Step | Pass? |
|---|------|-------|
| 1 | Wallet A connects → Me shows handle / Online | ☐ |
| 2 | Wallet B connects (other browser/profile) | ☐ |
| 3 | Both open **Gen Chat** → wait for **P2P · 1** (or Relay OK) | ☐ |
| 4 | A sends text → B sees it (toast/badge if mention `@`) | ☐ |
| 5 | A uploads image or short video in **#media** or Gen Chat (staff) → URL loads | ☐ |
| 6 | Paste a GIF / Tenor link → renders | ☐ |
| 7 | **Members**: A appears under **Online** (not Invisible) | ☐ |
| 8 | Staff: timeout or ban test on a throwaway if available | ☐ |
| 9 | **#tree**: water once → height ticks; second wallet can water after cooldown | ☐ |
| 10 | **#off-topic**: send a message | ☐ |
| 11 | **Log off** → Connect Wallet shows; reconnect works | ☐ |
| 12 | `GET /health` → `status=ok`, `media.ready=true`, `db.exists=true` | ☐ |

### Notes

- If P2P badge never appears: still OK if Relay delivers (hybrid). Note it.  
- If media fails on `s3`: check `/health` → `media.detail`.  
- If Members shows Offline for yourself while Me says Online: hard-refresh; report if still broken.

**Sign-off:** _____________ date _____________
