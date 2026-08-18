/**
 * Gen Chat P2P — WebRTC data channels between wallets.
 * Socket.IO = signaling ONLY. Message bodies over P2P when connected.
 */
(function (global) {
  'use strict';

  const LOCAL_KEY = 'lc_genchat_p2p_log';

  function GenChatP2P() {
    this.wallet = null;
    this.profile = null;
    this.socket = null;
    this.peers = new Map();
    this.onMessage = null;
    this.onStatus = null;
    this._joined = false;
    this._seenIds = new Set();
  }

  GenChatP2P.prototype._emitStatus = function () {
    let connected = 0;
    this.peers.forEach(function (p) {
      if (p.dc && p.dc.readyState === 'open') connected++;
    });
    const status = {
      mode: connected > 0 ? 'p2p' : 'relay',
      peers: this.peers.size,
      connected: connected,
    };
    if (typeof this.onStatus === 'function') this.onStatus(status);
    return status;
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
    this._emitStatus();
  };

  GenChatP2P.prototype.connectedCount = function () {
    let n = 0;
    this.peers.forEach(function (p) {
      if (p.dc && p.dc.readyState === 'open') n++;
    });
    return n;
  };

  GenChatP2P.prototype.send = function (msg) {
    const id =
      msg.id ||
      'p2p-' + Date.now() + '-' + Math.random().toString(36).slice(2, 9);
    msg.id = id;
    msg.transport = 'p2p';
    msg.created_at = msg.created_at || Math.floor(Date.now() / 1000);
    const payload = JSON.stringify(msg);
    let n = 0;
    this.peers.forEach(function (p) {
      if (p.dc && p.dc.readyState === 'open') {
        try {
          p.dc.send(payload);
          n++;
        } catch (e) {
          console.warn('[genchat-p2p] send', e);
        }
      }
    });
    if (n > 0) {
      this._seenIds.add(id);
      this._storeLocal(msg);
    }
    this._emitStatus();
    return n;
  };

  GenChatP2P.prototype._storeLocal = function (msg) {
    try {
      const arr = JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]');
      const id =
        msg.id ||
        msg.sender_wallet + ':' + msg.created_at + ':' + String(msg.content || '').slice(0, 24);
      if (arr.some(function (m) { return m.id === id; })) return;
      arr.push(msg);
      while (arr.length > 300) arr.shift();
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

  GenChatP2P.prototype._wireDc = function (remoteWallet, dc) {
    const entry = this.peers.get(remoteWallet);
    if (!entry) return;
    entry.dc = dc;
    const self = this;
    dc.binaryType = 'arraybuffer';
    dc.onopen = function () {
      self._emitStatus();
    };
    dc.onclose = function () {
      self._emitStatus();
    };
    dc.onerror = function () {
      self._emitStatus();
    };
    dc.onmessage = function (ev) {
      try {
        const msg = JSON.parse(ev.data);
        if (!msg || !msg.content) return;
        const id =
          msg.id ||
          msg.sender_wallet + ':' + msg.created_at + ':' + String(msg.content).slice(0, 24);
        if (self._seenIds.has(id)) return;
        self._seenIds.add(id);
        self._storeLocal(msg);
        if (typeof self.onMessage === 'function') self.onMessage(msg);
      } catch (e) {
        console.warn('[genchat-p2p] bad message', e);
      }
    };
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
          // glare — ignore if we're impolite
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
