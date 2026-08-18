/**
 * Channel P2P — WebRTC mesh for live chat channels (Gen Chat, #dev, …).
 * Socket.IO = signaling ONLY (who's here + SDP/ICE) per channel room.
 * Message bodies: datachannels. Gossip + history sync per channel.
 */
(function (global) {
  'use strict';

  function localKey(channel) {
    return 'lc_p2p_log_' + (channel || 'general');
  }
  const LOCAL_KEY_LEGACY = 'lc_genchat_p2p_log';
  const MAX_LOCAL = 400;
  const SYNC_LIMIT = 80;

  function GenChatP2P() {
    this.wallet = null;
    this.profile = null;
    this.channel = 'general';
    this.socket = null;
    this.peers = new Map();
    this.onMessage = null;
    this.onStatus = null;
    this.onDelete = null;
    this.onEdit = null;
    this._joined = false;
    this._seenIds = new Set();
    this._sockBound = false;
    this._meshTimer = null;
    this._hydrateSeen();
  }

  GenChatP2P.prototype._hydrateSeen = function () {
    try {
      if (this.channel === 'general') {
        try {
          const legacy = localStorage.getItem(LOCAL_KEY_LEGACY);
          const curKey = localKey('general');
          const cur = localStorage.getItem(curKey);
          if (legacy) {
            if (!cur) {
              localStorage.setItem(curKey, legacy);
            } else {
              // Merge legacy into current then drop legacy (fixes split history / ghost dupes)
              try {
                const a = JSON.parse(cur || '[]');
                const b = JSON.parse(legacy || '[]');
                const merged = this._dedupeList(a.concat(b));
                localStorage.setItem(curKey, JSON.stringify(merged));
              } catch (e2) {}
            }
            try { localStorage.removeItem(LOCAL_KEY_LEGACY); } catch (e3) {}
          }
        } catch (e) {}
      }
      this.dedupeLocal();
      const arr = this.loadLocal();
      const self = this;
      this._seenIds = new Set();
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

  /** Same sender + identical content within 2 minutes → treat as one message. */
  GenChatP2P.prototype._isContentDupe = function (a, b) {
    if (!a || !b) return false;
    const sa = String(a.sender_wallet || '').toLowerCase();
    const sb = String(b.sender_wallet || '').toLowerCase();
    if (!sa || sa !== sb) return false;
    if (String(a.content || '') !== String(b.content || '')) return false;
    if (!a.content) return false;
    const ta = a.created_at || 0;
    const tb = b.created_at || 0;
    return Math.abs(ta - tb) <= 120;
  };

  GenChatP2P.prototype._preferMsg = function (a, b) {
    // Prefer relay/server UUIDs over ephemeral p2p- ids
    const aid = String((a && a.id) || '');
    const bid = String((b && b.id) || '');
    const aP2p = aid.indexOf('p2p-') === 0;
    const bP2p = bid.indexOf('p2p-') === 0;
    if (aP2p && !bP2p) return b;
    if (!aP2p && bP2p) return a;
    return (a.created_at || 0) <= (b.created_at || 0) ? a : b;
  };

  GenChatP2P.prototype._dedupeList = function (list) {
    const out = [];
    const byId = {};
    const self = this;
    (list || []).forEach(function (m) {
      if (!m) return;
      const id = self._msgId(m);
      if (id && byId[id] != null) {
        const prev = out[byId[id]];
        out[byId[id]] = self._preferMsg(prev, m);
        return;
      }
      let dupeIdx = -1;
      for (let i = 0; i < out.length; i++) {
        if (self._isContentDupe(out[i], m)) {
          dupeIdx = i;
          break;
        }
      }
      if (dupeIdx >= 0) {
        const kept = self._preferMsg(out[dupeIdx], m);
        const oldId = self._msgId(out[dupeIdx]);
        if (oldId && byId[oldId] === dupeIdx) delete byId[oldId];
        out[dupeIdx] = kept;
        const kid = self._msgId(kept);
        if (kid) byId[kid] = dupeIdx;
        return;
      }
      const idx = out.length;
      out.push(m);
      if (id) byId[id] = idx;
    });
    out.sort(function (a, b) {
      return (a.created_at || 0) - (b.created_at || 0);
    });
    return out;
  };

  GenChatP2P.prototype._saveLocalArr = function (arr) {
    try {
      while (arr.length > MAX_LOCAL) arr.shift();
      localStorage.setItem(localKey(this.channel), JSON.stringify(arr));
      // Keep legacy key cleared so deletes aren't resurrected from the old store
      if (this.channel === 'general') {
        try { localStorage.removeItem(LOCAL_KEY_LEGACY); } catch (e) {}
      }
    } catch (e) {}
  };

  GenChatP2P.prototype.dedupeLocal = function () {
    try {
      const cleaned = this._dedupeList(this.loadLocal());
      this._saveLocalArr(cleaned);
      return cleaned;
    } catch (e) {
      return this.loadLocal();
    }
  };

  GenChatP2P.prototype.removeLocal = function (id) {
    if (!id) return;
    try {
      const arr = this.loadLocal();
      const target = arr.find(function (m) {
        return m && m.id === id;
      });
      const self = this;
      const next = arr.filter(function (m) {
        if (!m) return false;
        if (m.id === id) return false;
        if (target && self._isContentDupe(m, target)) return false;
        return true;
      });
      this._saveLocalArr(next);
      this._seenIds.delete(id);
      if (target) {
        const tid = this._msgId(target);
        if (tid) this._seenIds.delete(tid);
      }
    } catch (e) {}
  };

  GenChatP2P.prototype.editLocal = function (id, content) {
    if (!id) return;
    try {
      const arr = this.loadLocal();
      const now = Math.floor(Date.now() / 1000);
      arr.forEach(function (m) {
        if (m && m.id === id) {
          m.content = content;
          m.edited = true;
          m.edited_at = now;
        }
      });
      this._saveLocalArr(arr);
    } catch (e) {}
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
        this.socket.emit('genchat_p2p_join', { wallet: this.wallet, channel: this.channel || 'general' });
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
        socket.emit('genchat_p2p_join', { wallet: self.wallet, channel: self.channel || 'general' });
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

  GenChatP2P.prototype.join = async function (wallet, profile, channel) {
    const nextCh = (channel || this.channel || 'general').toLowerCase();
    if (this._joined && this.channel && this.channel !== nextCh) {
      this.leave();
    }
    this.channel = nextCh;
    this.wallet = (wallet || '').toLowerCase();
    this.profile = profile || {};
    this._seenIds = new Set();
    this._hydrateSeen();
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
        this.socket.emit('genchat_p2p_leave', { wallet: this.wallet, channel: this.channel || 'general' });
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
    msg.slug = msg.slug || this.channel || 'general';
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
      const arr = JSON.parse(localStorage.getItem(localKey(this.channel)) || '[]');
      const id = this._msgId(msg);
      const self = this;
      if (id && arr.some(function (m) { return (m.id || '') === id; })) return;
      // Skip content twin (same GIF/text from same sender within 2 min)
      if (arr.some(function (m) { return self._isContentDupe(m, msg); })) return;
      arr.push(msg);
      this._saveLocalArr(arr);
    } catch (e) {}
  };

  GenChatP2P.prototype.loadLocal = function () {
    try {
      return JSON.parse(localStorage.getItem(localKey(this.channel)) || '[]');
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
        channel: self.channel || 'general',
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
        this.removeLocal(env.id);
      } catch (e) {}
      if (typeof this.onDelete === 'function') this.onDelete(env.id, env.slug || 'general');
      // Gossip delete so the whole mesh drops it
      this._broadcastEnvelope(
        { v: 1, kind: 'delete', id: env.id, slug: env.slug || 'general', by: env.by || fromWallet },
        fromWallet
      );
      return;
    }

    if (env.kind === 'edit' && env.id && env.content != null) {
      try {
        this.editLocal(env.id, env.content);
      } catch (e) {}
      if (typeof this.onEdit === 'function') this.onEdit(env.id, env.content, env.slug || 'general');
      this._broadcastEnvelope(
        {
          v: 1,
          kind: 'edit',
          id: env.id,
          content: env.content,
          slug: env.slug || 'general',
          by: env.by || fromWallet,
        },
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
          channel: this.channel || 'general',
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
            channel: this.channel || 'general',
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
