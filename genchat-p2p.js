/**
 * Gen Chat P2P — WebRTC data channels between wallets.
 * Socket.IO is signaling ONLY (who’s online + SDP/ICE). Message bodies go peer-to-peer when connected.
 * Fallback: caller still uses Railway community API when no live P2P peers.
 */
(function (global) {
  'use strict';

  const ROOM = 'genchat_p2p';
  const LOCAL_KEY = 'lc_genchat_p2p_log';

  function GenChatP2P() {
    this.wallet = null;
    this.profile = null;
    this.socket = null;
    this.peers = new Map(); // wallet -> { pc, dc, makingOffer }
    this.onMessage = null; // (msg) => void
    this.onStatus = null; // ({ mode, peers, connected }) => void
    this._joined = false;
    this._iceReady = false;
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
    if (!socket || this.socket === socket) return;
    this.socket = socket;

    socket.on('genchat_p2p_peers', function (data) {
      const list = (data && data.peers) || [];
      list.forEach(function (w) {
        if (w && self.wallet && w !== self.wallet) self._ensurePeer(w, true);
      });
      self._emitStatus();
    });

    socket.on('genchat_p2p_peer_joined', function (data) {
      const w = (data && data.wallet) || '';
      if (w && self.wallet && w !== self.wallet) self._ensurePeer(w, true);
      self._emitStatus();
    });

    socket.on('genchat_p2p_peer_left', function (data) {
      const w = (data && data.wallet) || '';
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
        this._iceReady = true;
      } catch (e) {
        console.warn('[genchat-p2p] ICE', e);
      }
    }

    if (this.socket && this.socket.connected) {
      this.socket.emit('genchat_p2p_join', { wallet: this.wallet });
      this._joined = true;
    }
    this._emitStatus();
  };

  GenChatP2P.prototype.leave = function () {
    if (this.socket && this._joined) {
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

  /** Send to all open datachannels. Returns number of peers that got it. */
  GenChatP2P.prototype.send = function (msg) {
    const payload = JSON.stringify(
      Object.assign({}, msg, {
        v: 1,
        transport: 'p2p',
        ts: msg.created_at || Math.floor(Date.now() / 1000),
      })
    );
    let n = 0;
    this.peers.forEach(function (p) {
      if (p.dc && p.dc.readyState === 'open') {
        try {
          p.dc.send(payload);
          n++;
        } catch (e) {
          console.warn('[genchat-p2p] send fail', e);
        }
      }
    });
    if (n > 0) this._storeLocal(msg);
    this._emitStatus();
    return n;
  };

  GenChatP2P.prototype._storeLocal = function (msg) {
    try {
      const arr = JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]');
      arr.push(msg);
      while (arr.length > 200) arr.shift();
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
    // Lower wallet address is impolite (makes offer) — avoids glare
    return this.wallet < remoteWallet;
  };

  GenChatP2P.prototype._ensurePeer = async function (remoteWallet, mayOffer) {
    remoteWallet = (remoteWallet || '').toLowerCase();
    if (!remoteWallet || remoteWallet === this.wallet) return;
    if (this.peers.has(remoteWallet)) {
      const existing = this.peers.get(remoteWallet);
      if (mayOffer && this._impolite(remoteWallet) && existing.pc && !existing.makingOffer && (!existing.dc || existing.dc.readyState !== 'open')) {
        await this._makeOffer(remoteWallet);
      }
      return;
    }

    const pc = new RTCPeerConnection(
      typeof rtcPeerConfig === 'function' ? rtcPeerConfig() : { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] }
    );
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
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed' || pc.connectionState === 'disconnected') {
        // keep peer entry; may recover
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
    dc.onopen = function () {
      self._emitStatus();
    };
    dc.onclose = function () {
      self._emitStatus();
    };
    dc.onmessage = function (ev) {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.content) {
          self._storeLocal(msg);
          if (typeof self.onMessage === 'function') self.onMessage(msg);
        }
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
      if (!entry.dc) {
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
        await pc.setRemoteDescription(data.sdp);
      } else if (data.type === 'ice' && data.candidate) {
        try {
          await pc.addIceCandidate(data.candidate);
        } catch (e) {
          /* ignore */
        }
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
