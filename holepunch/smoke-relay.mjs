/**
 * Smoke via DHT WebSocket relay (browser-equivalent path).
 * Starts relay in-process, two relayed DHTs + Hyperswarm on one topic.
 *
 *   cd holepunch && node smoke-relay.mjs
 */
import { WebSocketServer, WebSocket } from 'ws'
import DHT from 'hyperdht'
import { relay } from '@hyperswarm/dht-relay'
import Stream from '@hyperswarm/dht-relay/ws'
import Hyperswarm from 'hyperswarm'
import crypto from 'crypto'
import b4a from 'b4a'

const PORT = 49455
const topic = crypto.createHash('sha256').update('lightchat:genchat:general').digest()

const dht = new DHT()
await dht.ready()
const wss = new WebSocketServer({ port: PORT })
wss.on('connection', (ws) => {
  relay(dht, new Stream(false, ws))
})
console.log('relay on', PORT)

function waitOpen(ws) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('ws timeout')), 8000)
    ws.on('open', () => { clearTimeout(t); resolve() })
    ws.on('error', (e) => { clearTimeout(t); reject(e) })
  })
}

async function makeSwarm() {
  const ws = new WebSocket('ws://127.0.0.1:' + PORT)
  await waitOpen(ws)
  const rdht = new (await import('@hyperswarm/dht-relay')).default(new Stream(true, ws))
  const swarm = new Hyperswarm({ dht: rdht })
  return { swarm, ws, rdht }
}

const a = await makeSwarm()
const b = await makeSwarm()

let got = false
b.swarm.on('connection', (conn) => {
  conn.on('data', (data) => {
    console.log('B got:', b4a.toString(data).trim())
    got = true
    finish(0)
  })
})

a.swarm.on('connection', (conn) => {
  console.log('A connected')
  conn.write(JSON.stringify({ v: 1, kind: 'chat', msg: { id: 'smoke-relay', content: 'holepunch-relay-ok' } }) + '\n')
})

a.swarm.join(topic, { server: true, client: true })
b.swarm.join(topic, { server: true, client: true })
await Promise.all([a.swarm.flush(), b.swarm.flush()])
console.log('flushed — waiting…')

const timer = setTimeout(() => {
  console.error('FAIL: no peer message in 20s')
  finish(1)
}, 20000)

async function finish(code) {
  clearTimeout(timer)
  try { await a.swarm.destroy() } catch (_) {}
  try { await b.swarm.destroy() } catch (_) {}
  try { await a.rdht.destroy() } catch (_) {}
  try { await b.rdht.destroy() } catch (_) {}
  try { a.ws.close() } catch (_) {}
  try { b.ws.close() } catch (_) {}
  try { wss.close() } catch (_) {}
  try { await dht.destroy() } catch (_) {}
  process.exit(got ? 0 : code)
}
