/**
 * Smoke two Hyperswarm peers through the PUBLIC production DHT relay.
 *   cd holepunch && node smoke-live.mjs
 */
import WebSocket from 'ws'
import DHT from '@hyperswarm/dht-relay'
import Stream from '@hyperswarm/dht-relay/ws'
import Hyperswarm from 'hyperswarm'
import crypto from 'crypto'
import b4a from 'b4a'

const RELAY = process.env.HOLEPUNCH_RELAY || 'wss://lightchat-holepunch-production.up.railway.app'
const topic = crypto.createHash('sha256').update('lightchat:genchat:general').digest()

function waitOpen(ws, ms = 12000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('ws open timeout')), ms)
    ws.once('open', () => { clearTimeout(t); resolve() })
    ws.once('error', (e) => { clearTimeout(t); reject(e) })
  })
}

async function make() {
  const ws = new WebSocket(RELAY)
  await waitOpen(ws)
  const dht = new DHT(new Stream(true, ws))
  const swarm = new Hyperswarm({ dht })
  return { ws, dht, swarm }
}

const a = await make()
const b = await make()
console.log('both WS open →', RELAY)

let got = false
b.swarm.on('connection', (conn) => {
  conn.on('data', (d) => {
    console.log('B got', b4a.toString(d).trim())
    got = true
    finish(0)
  })
})
a.swarm.on('connection', (conn) => {
  conn.write(JSON.stringify({ v: 1, kind: 'chat', msg: { id: 'live-smoke', content: 'hp-live-ok' } }) + '\n')
})

a.swarm.join(topic, { server: true, client: true })
b.swarm.join(topic, { server: true, client: true })
await Promise.race([
  Promise.all([a.swarm.flush(), b.swarm.flush()]),
  new Promise((_, rej) => setTimeout(() => rej(new Error('flush timeout')), 20000)),
]).catch((e) => console.warn(e.message))

const timer = setTimeout(() => { console.error('FAIL'); finish(1) }, 25000)
async function finish(code) {
  clearTimeout(timer)
  try { await a.swarm.destroy() } catch {}
  try { await b.swarm.destroy() } catch {}
  try { await a.dht.destroy() } catch {}
  try { await b.dht.destroy() } catch {}
  try { a.ws.close() } catch {}
  try { b.ws.close() } catch {}
  process.exit(got ? 0 : code)
}
