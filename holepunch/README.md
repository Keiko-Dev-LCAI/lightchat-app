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

## Enable in the app

1. Open LightChat → **Me**
2. Turn on **Holepunch spike (experimental)**
3. Set relay URL, e.g. `ws://127.0.0.1:49443` (or `wss://…` behind TLS)
4. Open Gen Chat — status chip can show **HP · N** when peers connect on that topic

## Topic

`sha256("lightchat:genchat:" + channelSlug)` → 32-byte Hyperswarm topic.

## Deploy note

Do **not** put this on the main Flask Procfile yet (Python Railway image). Run as a **second service** when ready, then point clients at that `wss://` URL.
