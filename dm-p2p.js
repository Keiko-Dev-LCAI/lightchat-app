/**
 * DM P2P — WebRTC datachannel between two wallets.
 * Socket.IO = signaling only. Message bodies over P2P when connected.
 * Local encrypted-at-rest is out of scope; localStorage log per pair.
 */
(function (global) {
  'use strict';

  function pairKey(a, b) {
    a = (a || '').toLowerCase();
    b = (b || '').toLowerCase();
    return a < b ? a + '|' + b : b + '|' + a;
  }

  function DmP2P() {
    this.wallet = null;
    this.socket = null;
    this.peers = new Map(); // remoteWallet -> {pc, dc, makingOffer}
    this.onMessage = null;
    this.onStatus = null;
    this._seen = new Set();
    this._sockBound = false;
  }

  DmP2P.prototype.attachSocket = function (socket) {
    const self = this;
    if (!socket) return;
    if (this.socket === socket && this._sockBound) return;
    this.socket = socket;
    this._sockBound = true;

    socket.on('connect', function () {
      if (self.wallet) socket.emit('dm_p2p_hello', { wallet: self.wallet });
    });

    socket.on('dm_p2p_signal', function (data) {
      self._onSignal(data || {});
    });

    socket.on('dm_p2p_peer_online', function (data) {
      const w = ((data && data.wallet) || '').toLowerCase();
      if (w && self.wallet && w !== self.wallet && self._wantPeer(w)) {
        self._ensurePeer(w, true);
      }
    });
  };

  DmP2P.prototype.setWallet = async function (wallet) {
    this.wallet = (wallet || '').toLowerCase();
    if (!this.wallet) return;
    if (typeof loadIceServers === 'function') {
      try {
        await loadIceServers();
      } catch (e) {}
    }
    if (this.socket && this.socket.connected) {
      this.socket.emit('dm_p2p_hello', { wallet: this.wallet });
    }
  };

  DmP2P.prototype._wantPeer = function (remote) {
    // Connect if we have local history or open chat with them, or they're a local friend
    try {
      if (typeof currentChat !== 'undefined' && currentChat && currentChat.wallet === remote) return true;
    } catch (e) {}
    try {
      const friends = JSON.parse(localStorage.getItem('lc_p2p_friends') || '[]');
      if (friends.some(function (f) { return (f.wallet || f) === remote; })) return true;
    } catch (e) {}
    try {
      const log = localStorage.getItem('lc_dm_p2p_' + pairKey(this.wallet, remote));
      if (log && log.length > 2) return true;
    } catch (e) {}
    return false;
  };

  DmP2P.prototype.openWith = async function (remoteWallet) {
    remoteWallet = (remoteWallet || '').toLowerCase();
    if (!this.wallet || !remoteWallet || remoteWallet === this.wallet) return;
    await this._ensurePeer(remoteWallet, true);
    this._emitStatus(remoteWallet);
  };

  DmP2P.prototype.connected = function (remoteWallet) {
    const e = this.peers.get((remoteWallet || '').toLowerCase());
    return !!(e && e.dc && e.dc.readyState === 'open');
  };

  DmP2P.prototype.send = function (remoteWallet, msg) {
    remoteWallet = (remoteWallet || '').toLowerCase();
    const entry = this.peers.get(remoteWallet);
    if (!entry || !entry.dc || entry.dc.readyState !== 'open') return false;
    msg = msg || {};
    msg.id = msg.id || 'dm-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    msg.transport = 'p2p';
    msg.created_at = msg.created_at || Math.floor(Date.now() / 1000);
    msg.sender_wallet = msg.sender_wallet || this.wallet;
    msg.recipient_wallet = remoteWallet;
    try {
      entry.dc.send(JSON.stringify({ v: 1, kind: 'dm', msg: msg }));
      this._seen.add(msg.id);
      this._store(remoteWallet, msg);
      return true;
    } catch (e) {
      return false;
    }
  };

  DmP2P.prototype.loadLocal = function (remoteWallet) {
    try {
      return JSON.parse(
        localStorage.getItem('lc_dm_p2p_' + pairKey(this.wallet, remoteWallet)) || '[]'
      );
    } catch (e) {
      return [];
    }
  };

  DmP2P.prototype._store = function (remoteWallet, msg) {
    try {
      const key = 'lc_dm_p2p_' + pairKey(this.wallet, remoteWallet);
      const arr = JSON.parse(localStorage.getItem(key) || '[]');
      if (arr.some(function (m) { return m.id === msg.id; })) return;
      arr.push(msg);
      while (arr.length > 500) arr.shift();
      localStorage.setItem(key, JSON.stringify(arr));
    } catch (e) {}
  };

  DmP2P.prototype._emitStatus = function (remote) {
    if (typeof this.onStatus === 'function') {
      this.onStatus({
        peer: remote,
        connected: this.connected(remote),
      });
    }
  };

  DmP2P.prototype._impolite = function (remote) {
    return this.wallet < remote;
  };

  DmP2P.prototype._ensurePeer = async function (remoteWallet, mayOffer) {
    remoteWallet = (remoteWallet || '').toLowerCase();
    if (!remoteWallet || !this.wallet || remoteWallet === this.wallet) return;

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
      self.socket.emit('dm_p2p_signal', {
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
      self._emitStatus(remoteWallet);
    };
    pc.ondatachannel = function (ev) {
      self._wireDc(remoteWallet, ev.channel);
    };

    if (mayOffer && this._impolite(remoteWallet)) {
      const dc = pc.createDataChannel('dm', { ordered: true });
      this._wireDc(remoteWallet, dc);
      await this._makeOffer(remoteWallet);
    }
  };

  DmP2P.prototype._wireDc = function (remoteWallet, dc) {
    const entry = this.peers.get(remoteWallet);
    if (!entry) return;
    entry.dc = dc;
    const self = this;
    dc.onopen = function () {
      self._emitStatus(remoteWallet);
    };
    dc.onclose = function () {
      self._emitStatus(remoteWallet);
    };
    dc.onmessage = function (ev) {
      try {
        let env = JSON.parse(ev.data);
        if (env && env.content && !env.kind) env = { kind: 'dm', msg: env };
        if (!env || env.kind !== 'dm' || !env.msg) return;
        const msg = env.msg;
        if (!msg.id) msg.id = 'dm-' + (msg.created_at || Date.now());
        if (self._seen.has(msg.id)) return;
        self._seen.add(msg.id);
        self._store(remoteWallet, msg);
        if (typeof self.onMessage === 'function') self.onMessage(msg, remoteWallet);
      } catch (e) {}
    };
  };

  DmP2P.prototype._makeOffer = async function (remoteWallet) {
    const entry = this.peers.get(remoteWallet);
    if (!entry || !entry.pc) return;
    entry.makingOffer = true;
    try {
      if (!entry.dc || entry.dc.readyState === 'closed') {
        const dc = entry.pc.createDataChannel('dm', { ordered: true });
        this._wireDc(remoteWallet, dc);
      }
      const offer = await entry.pc.createOffer();
      await entry.pc.setLocalDescription(offer);
      if (this.socket) {
        this.socket.emit('dm_p2p_signal', {
          from: this.wallet,
          to: remoteWallet,
          type: 'offer',
          sdp: entry.pc.localDescription,
        });
      }
    } catch (e) {
      console.warn('[dm-p2p] offer', e);
    }
    entry.makingOffer = false;
  };

  DmP2P.prototype._onSignal = async function (data) {
    const from = (data.from || '').toLowerCase();
    const to = (data.to || '').toLowerCase();
    if (!from || to !== this.wallet) return;
    await this._ensurePeer(from, false);
    const entry = this.peers.get(from);
    if (!entry || !entry.pc) return;
    const pc = entry.pc;
    try {
      if (data.type === 'offer' && data.sdp) {
        if (entry.makingOffer && this._impolite(from)) return;
        await pc.setRemoteDescription(data.sdp);
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        if (this.socket) {
          this.socket.emit('dm_p2p_signal', {
            from: this.wallet,
            to: from,
            type: 'answer',
            sdp: pc.localDescription,
          });
        }
      } else if (data.type === 'answer' && data.sdp) {
        if (!pc.currentRemoteDescription) await pc.setRemoteDescription(data.sdp);
      } else if (data.type === 'ice' && data.candidate) {
        try {
          await pc.addIceCandidate(data.candidate);
        } catch (e) {}
      }
    } catch (e) {
      console.warn('[dm-p2p] signal', e);
    }
    this._emitStatus(from);
  };

  /** Local friends (P2P-first). Server contacts remain optional sync. */
  DmP2P.prototype.addLocalFriend = function (friendWallet, meta) {
    friendWallet = (friendWallet || '').toLowerCase();
    if (!friendWallet) return;
    let list = [];
    try {
      list = JSON.parse(localStorage.getItem('lc_p2p_friends') || '[]');
    } catch (e) {}
    if (!list.some(function (f) { return (f.wallet || f) === friendWallet; })) {
      list.push({
        wallet: friendWallet,
        handle: (meta && meta.handle) || '',
        added_at: Math.floor(Date.now() / 1000),
      });
      localStorage.setItem('lc_p2p_friends', JSON.stringify(list));
    }
    this.openWith(friendWallet);
  };

  DmP2P.prototype.listLocalFriends = function () {
    try {
      return JSON.parse(localStorage.getItem('lc_p2p_friends') || '[]');
    } catch (e) {
      return [];
    }
  };

  global.DmP2P = DmP2P;
  global.dmP2P = global.dmP2P || new DmP2P();
})(window);
