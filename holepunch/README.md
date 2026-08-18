# LightChat Holepunch spike

Experimental **Hyperswarm / HyperDHT** path for Gen Chat.  
**Default chat path remains WebRTC** (`genchat-p2p.js`). This does not replace it.

## Why a relay?

Browsers cannot run the UDP DHT. Holepunch’s `@hyperswarm/dht-relay` tunnels DHT traffic over WebSocket so the browser can still use Hyperswarm topics + Noise streams.

> Upstream marks dht-relay as experimental — not for production.

## Run the relay (local)

```bash
cd holepunch
npm install
npm run relay
# listens on ws://0.0.0.0:49443
```

## Public relay (experimental)

- **HTTPS health:** https://lightchat-holepunch-production.up.railway.app/health  
- **WSS (browser):** `wss://lightchat-holepunch-production.up.railway.app`  
- Railway project: `lightchat-holepunch` (separate from Flask LightChat)

## Enable in the app

1. Open LightChat → **Me**
2. Turn on **Holepunch spike (experimental)**
3. Relay URL defaults to the public `wss://…` above (or set `ws://127.0.0.1:49443` for local)
4. Open Gen Chat — status can show **HP · N** when Hyperswarm peers connect

## Topic

`sha256("lightchat:genchat:" + channelSlug)` → 32-byte Hyperswarm topic.

## Redeploy relay

```bash
cd holepunch
railway up -d --ci
```
