/**
 * Gen Chat P2P — WebRTC mesh between wallets.
 * Socket.IO = signaling ONLY (who's here + SDP/ICE).
 * Message bodies: datachannels. Gossip + history sync so late joiners catch up
 * without the app server owning the log.
 */
(function (global) {
  'use strict';

  const LOCAL_KEY = 'lc_genchat_p2p_log';
  const MAX_LOCAL = 400;
  const SYNC_LIMIT = 80;

  function GenChatP2P() {
    this.wallet = null;
    this.profile = null;
    this.socket = null;
    this.peers = new Map();
    this.onMessage = null;
    this.onStatus = null;
    this._joined = false;
    this._seenIds = new Set();
    this._sockBound = false;
    this._meshTimer = null;
    this._hydrateSeen();
  }

  GenChatP2P.prototype._hydrateSeen = function () {
    try {
      const arr = this.loadLocal();
      const self = this;
      arr.forEach(function (m) {
        const id = self._msgId(m);
        if (id) self._seenIds.add(id);
      });
    } catch (e) {}
  };

  GenChatP2P.prototype._msgId = function (msg) {
    if (!msg) return '';
    return (
      msg.id ||
      (msg.sender_wallet || '') +
        ':' +
        (msg.created_at || '') +
        ':' +
        String(msg.content || '').slice(0, 24)
    );
  };

  GenChatP2P.prototype._emitStatus = function () {
    let connected = 0;
    const wallets = [];
    this.peers.forEach(function (p, w) {
      if (p.dc && p.dc.readyState === 'open') {
        connected++;
        wallets.push(w);
      }
    });
    const prev = this._lastConnected || 0;
    this._lastConnected = connected;
    // When mesh comes online, flood recent local history so offline-composed msgs spread
    if (prev === 0 && connected > 0) {
      try {
        this._floodRecent();
      } catch (e) {}
    }
    const status = {
      mode: connected > 0 ? 'p2p' : 'relay',
      peers: this.peers.size,
      connected: connected,
      wallets: wallets,
    };
    if (typeof this.onStatus === 'function') this.onStatus(status);
    return status;
  };

  GenChatP2P.prototype._floodRecent = function () {
    const msgs = this.loadLocal().slice(-60);
    const self = this;
    msgs.forEach(function (msg) {
      self._broadcastEnvelope({ v: 1, kind: 'gossip', msg: msg }, null);
    });
  };

  GenChatP2P.prototype.connectedWallets = function () {
    const out = [];
    this.peers.forEach(function (p, w) {
      if (p.dc && p.dc.readyState === 'open') out.push(w);
    });
    return out;
  };

  GenChatP2P.prototype._startMeshTimer = function () {
    const self = this;
    if (this._meshTimer) return;
    this._meshTimer = setInterval(function () {
      if (!self._joined) return;
      self._maintainMesh();
    }, 12000);
  };

  GenChatP2P.prototype._stopMeshTimer = function () {
    if (this._meshTimer) {
      clearInterval(this._meshTimer);
      this._meshTimer = null;
    }
  };

  /** Retry failed peers, re-join room, nudge sync on open channels */
  GenChatP2P.prototype._maintainMesh = function () {
    const self = this;
    if (this.socket && this.socket.connected && this.wallet) {
      try {
        this.socket.emit('genchat_p2p_join', { wallet: this.wallet });
      } catch (e) {}
    }
    this.peers.forEach(function (entry, w) {
      const open = entry.dc && entry.dc.readyState === 'open';
      if (open) {
        self._requestSync(w);
        self._pushRecent(w);
        return;
      }
      const state = entry.pc && entry.pc.connectionState;
      if (state === 'failed' || state === 'disconnected' || state === 'closed') {
        self._teardownPeer(w);
        self._ensurePeer(w, true);
      } else if (self._impolite(w) && !entry.makingOffer) {
        self._makeOffer(w);
      }
    });
    this._emitStatus();
  };

  GenChatP2P.prototype._pushRecent = function (remoteWallet) {
    const entry = this.peers.get(remoteWallet);
    if (!entry || !entry.dc || entry.dc.readyState !== 'open') return;
    const msgs = this.loadLocal().slice(-40);
    if (!msgs.length) return;
    try {
      entry.dc.send(
        JSON.stringify({ v: 1, kind: 'sync_res', msgs: msgs, from: this.wallet, push: true })
      );
    } catch (e) {}
  };

  GenChatP2P.prototype.attachSocket = function (socket) {
    const self = this;
    if (!socket) return;
    if (this.socket === socket && this._sockBound) return;
    this.socket = socket;
    this._sockBound = true;

    socket.on('connect', function () {
      if (self.wallet && self._joined) {
        socket.emit('genchat_p2p_join', { wallet: self.wallet });
      }
    });

    socket.on('genchat_p2p_peers', function (data) {
      const list = (data && data.peers) || [];
      list.forEach(function (w) {
        if (w && self.wallet && w !== self.wallet) self._ensurePeer(w, true);
      });
      self._emitStatus();
    });

    socket.on('genchat_p2p_peer_joined', function (data) {
      const w = ((data && data.wallet) || '').toLowerCase();
      if (w && self.wallet && w !== self.wallet) self._ensurePeer(w, true);
      self._emitStatus();
    });

    socket.on('genchat_p2p_peer_left', function (data) {
      const w = ((data && data.wallet) || '').toLowerCase();
      if (w) self._teardownPeer(w);
      self._emitStatus();
    });

    socket.on('genchat_p2p_signal', function (data) {
      self._onSignal(data || {});
    });
  };

  GenChatP2P.prototype.join = async function (wallet, profile) {
    this.wallet = (wallet || '').toLowerCase();
    this.profile = profile || {};
    if (!this.wallet) return;

    if (typeof loadIceServers === 'function') {
      try {
        await loadIceServers();
      } catch (e) {
        console.warn('[genchat-p2p] ICE', e);
      }
    }

    this._joined = true;
    if (this.socket && this.socket.connected) {
      this.socket.emit('genchat_p2p_join', { wallet: this.wallet });
    }
    this._startMeshTimer();
    this._emitStatus();
  };

  GenChatP2P.prototype.leave = function () {
    if (this.socket && this._joined && this.wallet) {
      try {
        this.socket.emit('genchat_p2p_leave', { wallet: this.wallet });
      } catch (e) {}
    }
    const self = this;
    Array.from(this.peers.keys()).forEach(function (w) {
      self._teardownPeer(w);
    });
    this._joined = false;
    this._stopMeshTimer();
    this._emitStatus();
  };

  GenChatP2P.prototype.connectedCount = function () {
    let n = 0;
    this.peers.forEach(function (p) {
      if (p.dc && p.dc.readyState === 'open') n++;
    });
    return n;
  };

  /** Broadcast chat msg to all open peers. Always stores locally. Returns peer count. */
  GenChatP2P.prototype.send = function (msg) {
    const id = this._msgId(msg) || 'p2p-' + Date.now() + '-' + Math.random().toString(36).slice(2, 9);
    msg.id = id;
    msg.transport = 'p2p';
    msg.created_at = msg.created_at || Math.floor(Date.now() / 1000);
    msg.slug = msg.slug || 'general';
    this._seenIds.add(id);
    this._storeLocal(msg);
    const n = this._broadcastEnvelope({ v: 1, kind: 'chat', msg: msg }, null);
    this._emitStatus();
    return n;
  };

  GenChatP2P.prototype._broadcastEnvelope = function (env, exceptWallet) {
    const payload = JSON.stringify(env);
    let n = 0;
    const self = this;
    this.peers.forEach(function (p, w) {
      if (exceptWallet && w === exceptWallet) return;
      if (p.dc && p.dc.readyState === 'open') {
        try {
          p.dc.send(payload);
          n++;
        } catch (e) {
          console.warn('[genchat-p2p] send', e);
        }
      }
    });
    return n;
  };

  GenChatP2P.prototype._storeLocal = function (msg) {
    try {
      const arr = JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]');
      const id = this._msgId(msg);
      if (arr.some(function (m) { return (m.id || '') === id; })) return;
      arr.push(msg);
      while (arr.length > MAX_LOCAL) arr.shift();
      localStorage.setItem(LOCAL_KEY, JSON.stringify(arr));
    } catch (e) {}
  };

  GenChatP2P.prototype.loadLocal = function () {
    try {
      return JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]');
    } catch (e) {
      return [];
    }
  };

  GenChatP2P.prototype._recentIds = function () {
    const arr = this.loadLocal();
    return arr.slice(-SYNC_LIMIT).map(function (m) {
      return m.id;
    }).filter(Boolean);
  };

  GenChatP2P.prototype._msgsMissing = function (haveIds) {
    const have = {};
    (haveIds || []).forEach(function (id) {
      have[id] = true;
    });
    return this.loadLocal()
      .filter(function (m) {
        return m.id && !have[m.id];
      })
      .slice(-SYNC_LIMIT);
  };

  GenChatP2P.prototype._impolite = function (remoteWallet) {
    return this.wallet < remoteWallet;
  };

  GenChatP2P.prototype._ensurePeer = async function (remoteWallet, mayOffer) {
    remoteWallet = (remoteWallet || '').toLowerCase();
    if (!remoteWallet || remoteWallet === this.wallet) return;

    if (this.peers.has(remoteWallet)) {
      const existing = this.peers.get(remoteWallet);
      const open = existing.dc && existing.dc.readyState === 'open';
      if (mayOffer && this._impolite(remoteWallet) && existing.pc && !existing.makingOffer && !open) {
        await this._makeOffer(remoteWallet);
      }
      return;
    }

    const cfg =
      typeof rtcPeerConfig === 'function'
        ? rtcPeerConfig()
        : { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };
    const pc = new RTCPeerConnection(cfg);
    const entry = { pc: pc, dc: null, makingOffer: false };
    this.peers.set(remoteWallet, entry);

    const self = this;
    pc.onicecandidate = function (ev) {
      if (!ev.candidate || !self.socket) return;
      self.socket.emit('genchat_p2p_signal', {
        from: self.wallet,
        to: remoteWallet,
        type: 'ice',
        candidate: ev.candidate,
      });
    };

    pc.onconnectionstatechange = function () {
      if (pc.connectionState === 'failed') {
        try {
          pc.restartIce();
        } catch (e) {}
      }
      self._emitStatus();
    };

    pc.ondatachannel = function (ev) {
      self._wireDc(remoteWallet, ev.channel);
    };

    if (mayOffer && this._impolite(remoteWallet)) {
      const dc = pc.createDataChannel('genchat', { ordered: true });
      this._wireDc(remoteWallet, dc);
      await this._makeOffer(remoteWallet);
    }

    this._emitStatus();
  };

  GenChatP2P.prototype._requestSync = function (remoteWallet) {
    const entry = this.peers.get(remoteWallet);
    if (!entry || !entry.dc || entry.dc.readyState !== 'open') return;
    try {
      entry.dc.send(
        JSON.stringify({
          v: 1,
          kind: 'sync_req',
          ids: this._recentIds(),
          from: this.wallet,
        })
      );
    } catch (e) {}
  };

  GenChatP2P.prototype._wireDc = function (remoteWallet, dc) {
    const entry = this.peers.get(remoteWallet);
    if (!entry) return;
    entry.dc = dc;
    const self = this;
    dc.binaryType = 'arraybuffer';
    dc.onopen = function () {
      self._emitStatus();
      self._requestSync(remoteWallet);
      self._pushRecent(remoteWallet);
    };
    dc.onclose = function () {
      self._emitStatus();
    };
    dc.onerror = function () {
      self._emitStatus();
    };
    dc.onmessage = function (ev) {
      self._onDcMessage(remoteWallet, ev.data);
    };
  };

  GenChatP2P.prototype._onDcMessage = function (fromWallet, raw) {
    let env;
    try {
      env = JSON.parse(raw);
    } catch (e) {
      return;
    }
    // Back-compat: bare chat message object
    if (env && env.content && !env.kind) {
      env = { v: 1, kind: 'chat', msg: env };
    }
    if (!env || !env.kind) return;

    if (env.kind === 'sync_req') {
      const missing = this._msgsMissing(env.ids || []);
      const entry = this.peers.get(fromWallet);
      if (entry && entry.dc && entry.dc.readyState === 'open') {
        try {
          entry.dc.send(
            JSON.stringify({ v: 1, kind: 'sync_res', msgs: missing, from: this.wallet })
          );
        } catch (e) {}
      }
      return;
    }

    if (env.kind === 'sync_res') {
      const self = this;
      (env.msgs || []).forEach(function (msg) {
        self._ingestChat(msg, fromWallet, false);
      });
      return;
    }

    if (env.kind === 'delete' && env.id) {
      try {
        const arr = this.loadLocal().filter(function (m) {
          return m.id !== env.id;
        });
        localStorage.setItem(LOCAL_KEY, JSON.stringify(arr));
        this._seenIds.delete(env.id);
      } catch (e) {}
      if (typeof this.onDelete === 'function') this.onDelete(env.id, env.slug || 'general');
      // Gossip delete so the whole mesh drops it
      this._broadcastEnvelope(
        { v: 1, kind: 'delete', id: env.id, slug: env.slug || 'general', by: env.by || fromWallet },
        fromWallet
      );
      return;
    }

    if (env.kind === 'chat' || env.kind === 'gossip') {
      this._ingestChat(env.msg || env, fromWallet, true);
    }
  };

  GenChatP2P.prototype._ingestChat = function (msg, fromWallet, doGossip) {
    if (!msg || !msg.content) return;
    const id = this._msgId(msg);
    if (!id || this._seenIds.has(id)) return;
    this._seenIds.add(id);
    msg.transport = msg.transport || 'p2p';
    this._storeLocal(msg);
    if (typeof this.onMessage === 'function') this.onMessage(msg);
    // Gossip to other peers so mesh fills gaps without the server
    if (doGossip) {
      this._broadcastEnvelope({ v: 1, kind: 'gossip', msg: msg }, fromWallet);
    }
  };

  GenChatP2P.prototype._makeOffer = async function (remoteWallet) {
    const entry = this.peers.get(remoteWallet);
    if (!entry || !entry.pc) return;
    entry.makingOffer = true;
    try {
      if (!entry.dc || entry.dc.readyState === 'closed') {
        const dc = entry.pc.createDataChannel('genchat', { ordered: true });
        this._wireDc(remoteWallet, dc);
      }
      const offer = await entry.pc.createOffer();
      await entry.pc.setLocalDescription(offer);
      if (this.socket) {
        this.socket.emit('genchat_p2p_signal', {
          from: this.wallet,
          to: remoteWallet,
          type: 'offer',
          sdp: entry.pc.localDescription,
        });
      }
    } catch (e) {
      console.warn('[genchat-p2p] offer', e);
    }
    entry.makingOffer = false;
  };

  GenChatP2P.prototype._onSignal = async function (data) {
    const from = (data.from || '').toLowerCase();
    const to = (data.to || '').toLowerCase();
    if (!from || to !== this.wallet) return;

    await this._ensurePeer(from, false);
    const entry = this.peers.get(from);
    if (!entry || !entry.pc) return;
    const pc = entry.pc;

    try {
      if (data.type === 'offer' && data.sdp) {
        if (entry.makingOffer) {
          if (this._impolite(from)) return;
        }
        await pc.setRemoteDescription(data.sdp);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        if (this.socket) {
          this.socket.emit('genchat_p2p_signal', {
            from: this.wallet,
            to: from,
            type: 'answer',
            sdp: pc.localDescription,
          });
        }
      } else if (data.type === 'answer' && data.sdp) {
        if (!pc.currentRemoteDescription) {
          await pc.setRemoteDescription(data.sdp);
        }
      } else if (data.type === 'ice' && data.candidate) {
        try {
          await pc.addIceCandidate(data.candidate);
        } catch (e) {}
      }
    } catch (e) {
      console.warn('[genchat-p2p] signal', e);
    }
    this._emitStatus();
  };

  GenChatP2P.prototype._teardownPeer = function (remoteWallet) {
    const entry = this.peers.get(remoteWallet);
    if (!entry) return;
    try {
      if (entry.dc) entry.dc.close();
    } catch (e) {}
    try {
      if (entry.pc) entry.pc.close();
    } catch (e) {}
    this.peers.delete(remoteWallet);
  };

  global.GenChatP2P = GenChatP2P;
  global.genChatP2P = global.genChatP2P || new GenChatP2P();
})(window);
