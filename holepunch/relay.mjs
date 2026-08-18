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
 *
 * Client sets localStorage lc_holepunch_relay = "ws://HOST:PORT"
 * (or wss:// when terminated TLS is in front).
 */
import { WebSocketServer } from 'ws'
import DHT from 'hyperdht'
import { relay } from '@hyperswarm/dht-relay'
import Stream from '@hyperswarm/dht-relay/ws'

const PORT = parseInt(process.env.HOLEPUNCH_PORT || process.env.PORT || '49443', 10)
const HOST = process.env.HOLEPUNCH_HOST || '0.0.0.0'

const dht = new DHT()
await dht.ready()

const wss = new WebSocketServer({ host: HOST, port: PORT })
console.log(`[holepunch-relay] listening ws://${HOST}:${PORT}`)
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

async function shutdown() {
  console.log('[holepunch-relay] shutting down')
  try { wss.close() } catch (_) {}
  try { await dht.destroy() } catch (_) {}
  process.exit(0)
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
