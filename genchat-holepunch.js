/**
 * LightChat — Holepunch / Hyperswarm spike (experimental).
 *
 * Browser talks HyperDHT through a WebSocket DHT relay
 * (@hyperswarm/dht-relay), then joins a Hyperswarm topic for the channel.
 *
 * Default Gen Chat path remains WebRTC (genchat-p2p.js). This module is
 * opt-in via localStorage lc_holepunch_on=1 (+ optional lc_holepunch_relay).
 *
 * Live relay default: wss://lightchat-holepunch-production.up.railway.app
 * Verified: Node↔Node chat over that relay works; browser still experimental
 * (esm.sh + WASM sodium may fail on some devices).
 */
(function (global) {
  'use strict';

  const ESM_DHT = 'https://esm.sh/@hyperswarm/dht-relay@0.4.3';
  const ESM_WS = 'https://esm.sh/@hyperswarm/dht-relay@0.4.3/ws';
  const ESM_SWARM = 'https://esm.sh/hyperswarm@4.11.7';
  const ESM_B4A = 'https://esm.sh/b4a@1.6.7';
  const DEFAULT_RELAY = 'wss://lightchat-holepunch-production.up.railway.app';
  const FLUSH_MS = 15000;
  const WS_MS = 12000;

  function withTimeout(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      const t = setTimeout(function () {
        reject(new Error((label || 'operation') + ' timeout'));
      }, ms);
      promise.then(
        function (v) { clearTimeout(t); resolve(v); },
        function (e) { clearTimeout(t); reject(e); }
      );
    });
  }

  function GenChatHolepunch() {
    this.wallet = null;
    this.channel = 'general';
    this.relayUrl = '';
    this.swarm = null;
    this.dht = null;
    this.ws = null;
    this.conns = new Map();
    this.onMessage = null;
    this.onStatus = null;
    this._joined = false;
    this._starting = false;
    this._error = '';
    this._seenIds = new Set();
    this._mods = null;
    this._reconnectTimer = null;
    this._wantJoin = false;
    this._joinGen = 0;
  }

  GenChatHolepunch.prototype._emitStatus = function () {
    const st = {
      mode: this._joined ? 'holepunch' : 'off',
      connected: this.conns.size,
      peers: this.conns.size,
      error: this._error || '',
      relay: this.relayUrl || '',
      experimental: true,
    };
    if (typeof this.onStatus === 'function') this.onStatus(st);
    return st;
  };

  GenChatHolepunch.prototype._loadMods = async function () {
    if (this._mods) return this._mods;
    try {
      const [DHT, StreamMod, Hyperswarm, b4a] = await withTimeout(
        Promise.all([
          import(/* webpackIgnore: true */ ESM_DHT),
          import(/* webpackIgnore: true */ ESM_WS),
          import(/* webpackIgnore: true */ ESM_SWARM),
          import(/* webpackIgnore: true */ ESM_B4A),
        ]),
        20000,
        'holepunch module load'
      );
      this._mods = {
        DHT: DHT.default || DHT,
        Stream: StreamMod.default || StreamMod,
        Hyperswarm: Hyperswarm.default || Hyperswarm,
        b4a: b4a.default || b4a,
      };
      return this._mods;
    } catch (e) {
      throw new Error('Could not load Hyperswarm in browser: ' + ((e && e.message) || e));
    }
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

  GenChatHolepunch.prototype._clearReconnect = function () {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  };

  GenChatHolepunch.prototype._scheduleReconnect = function () {
    const self = this;
    this._clearReconnect();
    if (!this._wantJoin || !this.isEnabled()) return;
    this._reconnectTimer = setTimeout(function () {
      self._reconnectTimer = null;
      if (!self._wantJoin || !self.isEnabled()) return;
      self.join(self.wallet, self.channel, self.relayUrl).catch(function () {});
    }, 4000);
  };

  GenChatHolepunch.prototype.leave = async function () {
    this._wantJoin = false;
    this._clearReconnect();
    this._joined = false;
    this._joinGen++;
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
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.onerror = null;
        this.ws.close();
      }
    } catch (e) {}
    this.ws = null;
    this._emitStatus();
  };

  GenChatHolepunch.prototype._wireConn = function (conn, info, mods) {
    const self = this;
    const key =
      info && info.publicKey
        ? mods.b4a.toString(info.publicKey, 'hex')
        : 'peer-' + Math.random().toString(36).slice(2, 8);
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

    const drop = function () {
      self.conns.delete(key);
      self._emitStatus();
    };
    conn.on('close', drop);
    conn.on('error', drop);

    try {
      const hello =
        JSON.stringify({
          v: 1,
          kind: 'hello',
          wallet: self.wallet,
          channel: self.channel,
        }) + '\n';
      self._writeConn(conn, hello, mods);
    } catch (e) {}
  };

  GenChatHolepunch.prototype._writeConn = function (conn, line, mods) {
    try {
      if (typeof conn.write === 'function') {
        // Prefer Uint8Array for Noise streams
        const payload = mods && mods.b4a ? mods.b4a.from(line) : line;
        conn.write(payload);
        return true;
      }
    } catch (e) {
      try {
        conn.write(line);
        return true;
      } catch (e2) {}
    }
    return false;
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
      this.swarm &&
      this.ws &&
      this.ws.readyState === 1
    ) {
      this._emitStatus();
      return true;
    }

    this._starting = true;
    this._wantJoin = true;
    this._error = 'Connecting to DHT relay…';
    this._emitStatus();
    await this.leave();
    this._wantJoin = true;
    this.wallet = wallet;
    this.channel = channel;
    this.relayUrl = relayUrl;
    const gen = ++this._joinGen;

    try {
      const mods = await this._loadMods();
      if (gen !== this._joinGen) return false;

      const ws = new WebSocket(relayUrl);
      this.ws = ws;
      await withTimeout(
        new Promise(function (resolve, reject) {
          ws.onopen = function () { resolve(); };
          ws.onerror = function () { reject(new Error('relay WebSocket error')); };
        }),
        WS_MS,
        'relay connect'
      );
      if (gen !== this._joinGen) return false;

      const self = this;
      ws.onclose = function () {
        self._joined = false;
        self._error = 'relay disconnected';
        self.conns.clear();
        self._emitStatus();
        self._scheduleReconnect();
      };

      const dht = new mods.DHT(new mods.Stream(true, ws));
      this.dht = dht;
      const swarm = new mods.Hyperswarm({ dht: dht });
      this.swarm = swarm;

      swarm.on('connection', function (conn, info) {
        self._wireConn(conn, info, mods);
      });

      const topic = await this.topicBytes(channel);
      const discovery = swarm.join(topic, { server: true, client: true });

      // flush can hang on some networks — don't block forever
      try {
        await withTimeout(swarm.flush(), FLUSH_MS, 'swarm flush');
      } catch (e) {
        console.warn('[holepunch] flush:', e && e.message);
        this._error = 'Joined relay — waiting for peers (flush slow)';
      }
      try {
        if (discovery && discovery.flushed) {
          await withTimeout(Promise.resolve(discovery.flushed()), 5000, 'discovery flush');
        }
      } catch (e) {}

      if (gen !== this._joinGen) return false;
      this._joined = true;
      if (!this._error || this._error.indexOf('Connecting') === 0 || this._error.indexOf('flush') >= 0) {
        this._error = this.conns.size ? '' : 'On topic — waiting for Hyperswarm peers';
      }
      this._emitStatus();
      return true;
    } catch (e) {
      console.warn('[holepunch] join failed', e);
      this._error = (e && e.message) || String(e);
      this._joined = false;
      try {
        await this.leave();
      } catch (e2) {}
      this._wantJoin = this.isEnabled();
      this._emitStatus();
      if (this._wantJoin) this._scheduleReconnect();
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
    const mods = this._mods;
    let n = 0;
    const self = this;
    this.conns.forEach(function (conn) {
      if (self._writeConn(conn, line, mods)) n++;
    });
    return n;
  };

  global.GenChatHolepunch = GenChatHolepunch;
  global.genChatHolepunch = global.genChatHolepunch || new GenChatHolepunch();
})(typeof window !== 'undefined' ? window : globalThis);
