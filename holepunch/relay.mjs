#!/usr/bin/env node
/**
 * LightChat Holepunch spike — Hyperswarm DHT relay over WebSocket.
 *
 * Browsers cannot speak UDP DHT directly. This relay lets a browser client use
 * @hyperswarm/dht-relay + Hyperswarm for topic discovery / Noise streams.
 *
 * Experimental — not production. Keep WebRTC Gen Chat as the default path.
 *
 * Usage:
 *   node relay.mjs
 *   HOLEPUNCH_PORT=49443 node relay.mjs
 *   PORT=8080 node relay.mjs   # Railway sets PORT
 *
 * Serves:
 *   GET /health  → 200 ok
 *   WS  /        → DHT relay stream
 */
import http from 'http'
import { WebSocketServer } from 'ws'
import DHT from 'hyperdht'
import { relay } from '@hyperswarm/dht-relay'
import Stream from '@hyperswarm/dht-relay/ws'

const PORT = parseInt(process.env.PORT || process.env.HOLEPUNCH_PORT || '49443', 10)
const HOST = process.env.HOLEPUNCH_HOST || '0.0.0.0'

const dht = new DHT()
await dht.ready()

const server = http.createServer((req, res) => {
  const path = (req.url || '/').split('?')[0]
  if (path === '/health' || path === '/') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      ok: true,
      service: 'lightchat-holepunch-relay',
      experimental: true,
    }))
    return
  }
  res.writeHead(404)
  res.end('not found')
})

const wss = new WebSocketServer({ server })
console.log(`[holepunch-relay] listening http+ws://${HOST}:${PORT}`)
console.log(`[holepunch-relay] dht key ${dht.defaultKeyPair.publicKey.toString('hex').slice(0, 16)}…`)

wss.on('connection', (ws, req) => {
  const ip = req.socket.remoteAddress || '?'
  console.log(`[holepunch-relay] client ${ip}`)
  try {
    relay(dht, new Stream(false, ws))
  } catch (e) {
    console.error('[holepunch-relay] relay error', e)
    try { ws.close() } catch (_) {}
  }
})

wss.on('error', (e) => console.error('[holepunch-relay] wss', e))

server.listen(PORT, HOST)

async function shutdown() {
  console.log('[holepunch-relay] shutting down')
  try { wss.close() } catch (_) {}
  try { server.close() } catch (_) {}
  try { await dht.destroy() } catch (_) {}
  process.exit(0)
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
