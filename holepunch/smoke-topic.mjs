/**
 * Node-only smoke: two Hyperswarm peers on the same topic (UDP DHT, no browser).
 * Proves the Holepunch stack works on this machine.
 *
 *   cd holepunch && node smoke-topic.mjs
 */
import Hyperswarm from 'hyperswarm'
import crypto from 'crypto'
import b4a from 'b4a'

const topic = crypto.createHash('sha256').update('lightchat:genchat:general').digest()
console.log('topic', topic.toString('hex').slice(0, 16) + '…')

const a = new Hyperswarm()
const b = new Hyperswarm()

let got = false
b.on('connection', (conn) => {
  conn.on('data', (data) => {
    const s = b4a.toString(data)
    console.log('B got:', s.trim())
    got = true
    shutdown()
  })
})

a.on('connection', (conn) => {
  console.log('A connected — sending ping')
  conn.write(JSON.stringify({ v: 1, kind: 'chat', msg: { id: 'smoke-1', content: 'holepunch-ok' } }) + '\n')
})

a.join(topic, { server: true, client: true })
b.join(topic, { server: true, client: true })
await Promise.all([a.flush(), b.flush()])
console.log('joined + flushed — waiting for connection…')

const t = setTimeout(() => {
  console.error('FAIL: no message in 15s (NAT/firewall may block UDP DHT)')
  shutdown(1)
}, 15000)

async function shutdown(code = 0) {
  clearTimeout(t)
  try { await a.destroy() } catch (_) {}
  try { await b.destroy() } catch (_) {}
  process.exit(got ? 0 : code)
}
