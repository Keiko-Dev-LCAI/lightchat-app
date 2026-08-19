# LightChat — 2-wallet staging smoke

**Pass this before telling hosts / eng that staging is ready.**  
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
| 13 | **#ask-ai**: "what proposals are live?" → lists real DAO titles/tallies (matches dao.lightchain.ai) | ☐ |
| 14 | **#ask-ai**: "explain #375517" or Safety Framework → grounded description + For/Against LCAI | ☐ |
| 15 | LIGHTCHAIN ▾: server items vs personal (Friends/DMs under avatar menu); staff see Server Settings | ☐ |
| 16 | Staff: Server Settings → Profile save persists; Roles/Members/Bans/Emoji tabs load | ☐ |
| 17 | Non-staff: no Server Settings entry; POST /server-profile → 403 | ☐ |
| 18 | Server Settings Roles/Bans show **short** wallets only; click copies full | ☐ |
| 19 | Backgrounded user with push on: @mention in a channel → web push; @everyone → no mass push | ☐ |
| 20 | Ticket reply: other party gets push; sender does not | ☐ |
| 21 | #ask-ai: "what's LCAI at?" → live USD; balance of a 0x… → LCAI amount | ☐ |
| 22 | #ask-ai: liquidity/pool → N/A or reserves (DEX may have 0 pairs); never invent | ☐ |
| 23 | Add friend → clear storage / other device same wallet → friend still listed | ☐ |
| 24 | save-friend does not notify the other party; un-friend clears server + local | ☐ |
| 25 | #ask-ai: ask → "thinking…" shows → answer clears it; media/bot post → no spinner | ☐ |
| 26 | #ask-ai: leave channel / ~200s timeout → no stuck thinking row | ☐ |

### Notes

- If P2P badge never appears: still OK if Relay delivers (**P2P-accelerated client-server** hybrid). Note it.  
- If media fails on `s3`: check `/health` → `media.detail`.  
- If Members shows Offline for yourself while Me says Online: hard-refresh; report if still broken.

**Sign-off:** _____________ date _____________
