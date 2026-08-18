# LightChat — 3+ wallet soak checklist

**Updated:** 2026-08-18 · KeikoDev  
**Goal:** Prove Discord-replace day-to-day reliability (Jason Challenge #2) with **at least three** Lightchain wallets on chain **9200**.

Hard-refresh the PWA (`lightchat.chat`) so `lc-build` matches latest. Use three browsers / profiles / devices (A, B, C).

---

## Setup

| Wallet | Browser / device | Notes |
|--------|------------------|-------|
| A | | Prefer one mobile PWA |
| B | | |
| C | | |

Confirm `GET https://web-production-bc64f.up.railway.app/health` → `status=ok`, `media.backend=fs`, `media.ready=true`.

---

## Checklist

| # | Step | Pass? |
|---|------|-------|
| 1 | A, B, C each connect (Lightchain 9200 only) | ☐ |
| 2 | All three open **Gen Chat** `#general` | ☐ |
| 3 | A sends text → B and C see it (Relay and/or P2P) | ☐ |
| 4 | B replies (reply bar) → A and C see quote | ☐ |
| 5 | C reacts with emoji → others see pill; C toggles off | ☐ |
| 6 | A uploads image/GIF → URL loads for B and C (prefer `/media/…`) | ☐ |
| 7 | Switch A to `#off-topic`, send → only that channel; `#general` unchanged | ☐ |
| 8 | **Members**: connected wallets under **Online** (not Invisible) | ☐ |
| 9 | A sets **Invisible** → B/C see A offline; A still works | ☐ |
| 10 | A back to **Online** → appears Online again within ~a few seconds | ☐ |
| 11 | Minimize MetaMask / blur tab 30s → return → no re-sign prompt; still in channel | ☐ |
| 12 | Refresh one client mid-chat → history present, **no duplicate** bubbles | ☐ |
| 13 | Delete own message → gone for self; does not resurrect after refresh | ☐ |
| 14 | Mobile: composer stays usable above keyboard; `#` opens Channels | ☐ |
| 15 | Third wallet C offline → A/B still chat; C reconnects and catches history | ☐ |

---

## Notes / failures

- P2P badge optional if Relay delivers (hybrid OK).  
- Presence empty when nobody connected is correct.  
- If duplicates appear: note sender, content type, and whether ids were `p2p-*` vs UUID.

**Sign-off:** _____________ date _____________
