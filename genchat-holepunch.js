/**
 * LightChat — Holepunch / Hyperswarm spike (experimental).
 *
 * Browser talks HyperDHT through a WebSocket DHT relay
 * (@hyperswarm/dht-relay), then joins a Hyperswarm topic for the channel.
 *
 * Default Gen Chat path remains WebRTC (genchat-p2p.js). This module is
 * opt-in via localStorage lc_holepunch_on=1 + lc_holepunch_relay=ws(s)://…
 */
(function (global) {
  'use strict';

  const ESM_DHT = 'https://esm.sh/@hyperswarm/dht-relay@0.4.3';
  const ESM_WS = 'https://esm.sh/@hyperswarm/dht-relay@0.4.3/ws';
  const ESM_SWARM = 'https://esm.sh/hyperswarm@4.11.7';
  const ESM_B4A = 'https://esm.sh/b4a@1.6.7';
  /** Public experimental DHT relay (Railway). Override via Me → relay URL. */
  const DEFAULT_RELAY = 'wss://lightchat-holepunch-production.up.railway.app';

  function GenChatHolepunch() {
    this.wallet = null;
    this.channel = 'general';
    this.relayUrl = '';
    this.swarm = null;
    this.dht = null;
    this.ws = null;
    this.conns = new Map(); // remoteKeyHex -> conn
    this.onMessage = null;
    this.onStatus = null;
    this._joined = false;
    this._starting = false;
    this._error = '';
    this._seenIds = new Set();
    this._mods = null;
  }

  GenChatHolepunch.prototype._emitStatus = function () {
    const st = {
      mode: this._joined ? 'holepunch' : 'off',
      connected: this.conns.size,
      peers: this.conns.size,
      error: this._error || '',
      relay: this.relayUrl || '',
    };
    if (typeof this.onStatus === 'function') this.onStatus(st);
    return st;
  };

  GenChatHolepunch.prototype._loadMods = async function () {
    if (this._mods) return this._mods;
    const [DHT, StreamMod, Hyperswarm, b4a] = await Promise.all([
      import(/* webpackIgnore: true */ ESM_DHT),
      import(/* webpackIgnore: true */ ESM_WS),
      import(/* webpackIgnore: true */ ESM_SWARM),
      import(/* webpackIgnore: true */ ESM_B4A),
    ]);
    this._mods = {
      DHT: DHT.default || DHT,
      Stream: StreamMod.default || StreamMod,
      Hyperswarm: Hyperswarm.default || Hyperswarm,
      b4a: b4a.default || b4a,
    };
    return this._mods;
  };

  GenChatHolepunch.prototype.topicBytes = async function (channel) {
    const mods = await this._loadMods();
    const label = 'lightchat:genchat:' + (channel || 'general');
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(label));
    return mods.b4a.from(digest);
  };

  GenChatHolepunch.prototype.isEnabled = function () {
    try {
      return localStorage.getItem('lc_holepunch_on') === '1';
    } catch (e) {
      return false;
    }
  };

  GenChatHolepunch.prototype.getRelayUrl = function () {
    try {
      const custom = (localStorage.getItem('lc_holepunch_relay') || '').trim();
      return custom || DEFAULT_RELAY;
    } catch (e) {
      return DEFAULT_RELAY;
    }
  };

  GenChatHolepunch.prototype.defaultRelayUrl = function () {
    return DEFAULT_RELAY;
  };

  GenChatHolepunch.prototype.setEnabled = function (on) {
    try {
      localStorage.setItem('lc_holepunch_on', on ? '1' : '0');
    } catch (e) {}
  };

  GenChatHolepunch.prototype.setRelayUrl = function (url) {
    try {
      localStorage.setItem('lc_holepunch_relay', String(url || '').trim());
    } catch (e) {}
  };

  GenChatHolepunch.prototype.connectedCount = function () {
    return this.conns.size;
  };

  GenChatHolepunch.prototype.leave = async function () {
    this._joined = false;
    try {
      this.conns.forEach(function (c) {
        try { c.destroy(); } catch (e) {}
      });
    } catch (e) {}
    this.conns.clear();
    try {
      if (this.swarm) await this.swarm.destroy();
    } catch (e) {}
    this.swarm = null;
    try {
      if (this.dht) await this.dht.destroy();
    } catch (e) {}
    this.dht = null;
    try {
      if (this.ws) this.ws.close();
    } catch (e) {}
    this.ws = null;
    this._emitStatus();
  };

  GenChatHolepunch.prototype.join = async function (wallet, channel, relayUrl) {
    wallet = (wallet || '').toLowerCase();
    channel = (channel || 'general').toLowerCase();
    relayUrl = (relayUrl != null ? relayUrl : this.getRelayUrl()).trim();
    if (!wallet) {
      this._error = 'wallet required';
      this._emitStatus();
      return false;
    }
    if (!relayUrl) {
      this._error = 'Set a DHT relay URL (ws:// or wss://)';
      this._emitStatus();
      return false;
    }
    if (this._starting) return false;
    if (
      this._joined &&
      this.wallet === wallet &&
      this.channel === channel &&
      this.relayUrl === relayUrl &&
      this.swarm
    ) {
      this._emitStatus();
      return true;
    }

    this._starting = true;
    this._error = '';
    await this.leave();
    this.wallet = wallet;
    this.channel = channel;
    this.relayUrl = relayUrl;

    try {
      const mods = await this._loadMods();
      const ws = new WebSocket(relayUrl);
      this.ws = ws;
      await new Promise(function (resolve, reject) {
        const t = setTimeout(function () {
          reject(new Error('relay connect timeout'));
        }, 12000);
        ws.onopen = function () {
          clearTimeout(t);
          resolve();
        };
        ws.onerror = function () {
          clearTimeout(t);
          reject(new Error('relay WebSocket error'));
        };
      });

      const dht = new mods.DHT(new mods.Stream(true, ws));
      this.dht = dht;
      const swarm = new mods.Hyperswarm({ dht: dht });
      this.swarm = swarm;

      const self = this;
      swarm.on('connection', function (conn, info) {
        const key = info && info.publicKey ? mods.b4a.toString(info.publicKey, 'hex') : 'peer';
        self.conns.set(key, conn);
        self._emitStatus();

        let buf = '';
        conn.on('data', function (data) {
          try {
            buf += typeof data === 'string' ? data : mods.b4a.toString(data);
            let idx;
            while ((idx = buf.indexOf('\n')) >= 0) {
              const line = buf.slice(0, idx).trim();
              buf = buf.slice(idx + 1);
              if (!line) continue;
              let env;
              try {
                env = JSON.parse(line);
              } catch (e) {
                continue;
              }
              if (!env || env.v !== 1 || env.kind !== 'chat' || !env.msg) continue;
              const msg = env.msg;
              const id = msg.id || '';
              if (id && self._seenIds.has(id)) continue;
              if (id) self._seenIds.add(id);
              msg.transport = 'holepunch';
              msg.slug = msg.slug || self.channel;
              if (typeof self.onMessage === 'function') self.onMessage(msg);
            }
          } catch (e) {
            console.warn('[holepunch] data', e);
          }
        });

        conn.on('close', function () {
          self.conns.delete(key);
          self._emitStatus();
        });
        conn.on('error', function () {
          self.conns.delete(key);
          self._emitStatus();
        });

        // Hello so peers know we're here
        try {
          const hello = JSON.stringify({
            v: 1,
            kind: 'hello',
            wallet: self.wallet,
            channel: self.channel,
          }) + '\n';
          conn.write(hello);
        } catch (e) {}
      });

      const topic = await this.topicBytes(channel);
      const discovery = swarm.join(topic, { server: true, client: true });
      await swarm.flush();
      try {
        if (discovery && discovery.flushed) await discovery.flushed();
      } catch (e) {}

      this._joined = true;
      this._error = '';
      this._emitStatus();
      return true;
    } catch (e) {
      console.warn('[holepunch] join failed', e);
      this._error = (e && e.message) || String(e);
      this._joined = false;
      try {
        await this.leave();
      } catch (e2) {}
      this._emitStatus();
      return false;
    } finally {
      this._starting = false;
    }
  };

  GenChatHolepunch.prototype.send = function (msg) {
    if (!msg || !this._joined || !this.conns.size) return 0;
    if (!msg.id) {
      msg.id = 'hp-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    }
    msg.transport = 'holepunch';
    msg.slug = msg.slug || this.channel;
    msg.created_at = msg.created_at || Math.floor(Date.now() / 1000);
    this._seenIds.add(msg.id);
    const line = JSON.stringify({ v: 1, kind: 'chat', msg: msg }) + '\n';
    let n = 0;
    this.conns.forEach(function (conn) {
      try {
        conn.write(line);
        n++;
      } catch (e) {}
    });
    return n;
  };

  global.GenChatHolepunch = GenChatHolepunch;
  global.genChatHolepunch = global.genChatHolepunch || new GenChatHolepunch();
})(typeof window !== 'undefined' ? window : globalThis);
