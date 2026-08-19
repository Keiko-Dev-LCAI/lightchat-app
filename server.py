import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, make_response, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_cors import CORS
import sqlite3
import os
import time
import re
import threading
import json
import base64
import uuid
import secrets
import subprocess
import tempfile


def _load_local_env():
    """Load key=value pairs from a local .env into os.environ (no overwrite).
    Railway/production already injects env; this is for local runs only."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                if not key or key in os.environ:
                    continue
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                os.environ[key] = val
    except OSError as e:
        print(f'[lightchat] .env load skipped: {e}', flush=True)


_load_local_env()

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False

# Web-push keys — from env only (never hardcode). Push disables cleanly if unset.
VAPID_PUBLIC_KEY = (os.environ.get('VAPID_PUBLIC_KEY') or '').strip()
VAPID_PRIVATE_KEY = (os.environ.get('VAPID_PRIVATE_KEY') or '').strip()
_vapid_claims_env = (os.environ.get('VAPID_CLAIMS') or '').strip()
if _vapid_claims_env:
    try:
        VAPID_CLAIMS = json.loads(_vapid_claims_env)
    except Exception:
        sub = _vapid_claims_env if _vapid_claims_env.startswith('mailto:') else f'mailto:{_vapid_claims_env}'
        VAPID_CLAIMS = {'sub': sub}
else:
    VAPID_CLAIMS = {'sub': 'mailto:noreply@lightchat.app'}
WEB_PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and PUSH_AVAILABLE)
if not WEB_PUSH_ENABLED:
    print('[lightchat] web push disabled — set VAPID_PUBLIC_KEY + VAPID_PRIVATE_KEY (and install pywebpush)', flush=True)

app = Flask(__name__)
CORS(app, origins="*")
_secret = (os.environ.get('SECRET_KEY') or '').strip()
if not _secret:
    raise RuntimeError('SECRET_KEY env var is required — refuse to start with a default secret')
app.config['SECRET_KEY'] = _secret
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max request
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

_data_dir = os.environ.get('DATA_DIR', '/app/data')
os.makedirs(_data_dir, exist_ok=True)
DB_PATH = os.environ.get('DB_PATH', os.path.join(_data_dir, 'lightchat.db'))
print(f"[lightchat] DATA_DIR={_data_dir} DB_PATH={DB_PATH}", flush=True)

# AIVM protocol — aligned with lightchain-protocol/lcai-chat-v2 mainnet (config/index.ts)
# Reference: https://github.com/lightchain-protocol/lcai-chat-v2
_AIVM_PROTOCOL   = "lcai-chat-v2-mainnet"
_AIVM_GATEWAY    = "https://chat-api.mainnet.lightchain.ai"
_AIVM_RELAY      = "wss://relay.mainnet.lightchain.ai/ws"
_AIVM_RPC        = "https://rpc.mainnet.lightchain.ai"
_AIVM_JOB_REG    = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
_AIVM_AI_CFG     = "0x24D11533C354092ed6E18b964257819cE78Ce77D"
_AIVM_WORKER_REG = "0x0000000000000000000000000000000000001002"
_AIVM_JOB_FEE    = 20_000_000_000_000_000   # 0.02 LCAI in wei
_AIVM_CHAIN_ID   = 9200

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=30000')
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS handles (
            wallet TEXT PRIMARY KEY,
            handle TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts (
            wallet TEXT NOT NULL,
            contact_wallet TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (wallet, contact_wallet)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender_wallet TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT NOT NULL DEFAULT 'text',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            wallet TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            subscription TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (wallet, endpoint)
        );
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            image_data TEXT,
            image_type TEXT,
            storage_type TEXT NOT NULL DEFAULT 'cloud',
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS chat_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            image_data TEXT NOT NULL,
            image_type TEXT NOT NULL DEFAULT 'image/jpeg',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_data TEXT NOT NULL,
            file_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            file_size INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voice_data TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'audio/mpeg',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS call_usage (
            wallet TEXT PRIMARY KEY,
            free_calls_used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            wallet TEXT PRIMARY KEY,
            expires_at INTEGER,
            tx_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            avatar TEXT
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (group_id, wallet)
        );
        CREATE TABLE IF NOT EXISTS group_messages (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            sender_wallet TEXT NOT NULL,
            content TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'text',
            timestamp INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calendar_events (
            id TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            title TEXT NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#9b7fe8',
            shared_with TEXT NOT NULL DEFAULT '[]',
            reminder_minutes INTEGER NOT NULL DEFAULT 30
        );
        -- Persist sign-in sessions across Railway restarts (Bearer token in localStorage)
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'signed',
            exp INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_wallet ON auth_sessions(wallet);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_exp ON auth_sessions(exp);
        -- Desktop→mobile one-time link codes (3 min, single-use)
        CREATE TABLE IF NOT EXISTS auth_handoff_codes (
            code TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'signed',
            exp INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_auth_handoff_exp ON auth_handoff_codes(exp);
    ''')
    conn.commit()
    conn.close()

init_db()
try:
    migrate_strip_at_handles()
except Exception as _e:
    print(f'  [handles] migrate @ strip failed: {_e}')

# Migrate memories table: add media_type column if it doesn't exist yet
try:
    _mig_conn = get_db()
    _mig_conn.execute('ALTER TABLE memories ADD COLUMN media_type TEXT NOT NULL DEFAULT "image"')
    _mig_conn.commit()
    _mig_conn.close()
except Exception:
    pass  # Column already exists

# Migrate memories table: add chain_memory_id column for on-chain storage
try:
    _mig_conn = get_db()
    _mig_conn.execute('ALTER TABLE memories ADD COLUMN chain_memory_id INTEGER')
    _mig_conn.commit()
    _mig_conn.close()
except Exception:
    pass  # Column already exists

def cleanup_messages():
    while True:
        time.sleep(60)
        try:
            conn = get_db()
            conn.execute('DELETE FROM messages WHERE expires_at < ?', (int(time.time()),))
            conn.execute('DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?', (int(time.time()),))
            conn.execute('DELETE FROM chat_images WHERE expires_at < ?', (int(time.time()),))
            conn.execute('DELETE FROM chat_files WHERE expires_at < ?', (int(time.time()),))
            conn.execute('DELETE FROM chat_voices WHERE expires_at < ?', (int(time.time()),))
            conn.execute('DELETE FROM group_messages WHERE expires_at < ?', (int(time.time()),))
            conn.commit()
            conn.close()
            # Clean up expired in-memory voice messages
            now_ts = int(time.time())
            with _voice_lock:
                expired = [k for k, v in _voice_store.items() if v['expires_at'] < now_ts]
                for k in expired:
                    del _voice_store[k]
            # Clean up expired in-memory video messages
            with _video_lock:
                expired_v = [k for k, v in _video_store.items() if v['expires_at'] < now_ts]
                for k in expired_v:
                    del _video_store[k]
        except Exception:
            pass

threading.Thread(target=cleanup_messages, daemon=True).start()

# Sentinel value meaning "this message never expires" (year 3000).
# Using a far-future timestamp avoids schema changes while keeping
# all existing expires_at > now queries working correctly.
NEVER_EXPIRES = 32503680000

# ── Media storage (handoff: prefer fs or s3 — not SQLite forever) ──
# See MEDIA-STORAGE.md / RUNBOOK.md
# MEDIA_BACKEND=local (SQLite legacy) | fs (DATA_DIR/media) | s3 (R2/S3)
# s3 needs: MEDIA_S3_ENDPOINT?, MEDIA_S3_BUCKET, MEDIA_S3_ACCESS_KEY, MEDIA_S3_SECRET_KEY, MEDIA_S3_PUBLIC_BASE
MEDIA_BACKEND = (os.environ.get('MEDIA_BACKEND') or 'local').strip().lower()
MEDIA_MAX_IMAGE_MB = int(os.environ.get('MEDIA_MAX_IMAGE_MB') or '8')
MEDIA_MAX_VIDEO_MB = int(os.environ.get('MEDIA_MAX_VIDEO_MB') or '20')
MEDIA_FS_DIR = os.environ.get('MEDIA_FS_DIR') or os.path.join(_data_dir, 'media')


def media_backend_status() -> dict:
    """Health / runbook helper — what media mode is active."""
    st = {
        'backend': MEDIA_BACKEND,
        'max_image_mb': MEDIA_MAX_IMAGE_MB,
        'max_video_mb': MEDIA_MAX_VIDEO_MB,
        'ready': True,
        'detail': '',
    }
    if MEDIA_BACKEND == 's3':
        need = ['MEDIA_S3_BUCKET', 'MEDIA_S3_ACCESS_KEY', 'MEDIA_S3_SECRET_KEY', 'MEDIA_S3_PUBLIC_BASE']
        missing = [k for k in need if not (os.environ.get(k) or '').strip()]
        try:
            import boto3  # noqa: F401
            has_boto = True
        except ImportError:
            has_boto = False
        if missing or not has_boto:
            st['ready'] = False
            st['detail'] = ('missing ' + ','.join(missing)) if missing else 'boto3 not installed'
        else:
            st['detail'] = 'R2/S3 configured'
    elif MEDIA_BACKEND == 'fs':
        try:
            os.makedirs(MEDIA_FS_DIR, exist_ok=True)
            st['detail'] = MEDIA_FS_DIR
            st['ready'] = os.path.isdir(MEDIA_FS_DIR) and os.access(MEDIA_FS_DIR, os.W_OK)
        except Exception as e:
            st['ready'] = False
            st['detail'] = str(e)[:120]
    else:
        st['detail'] = 'SQLite chat_images/chat_files (legacy bridge)'
    return st


def media_store_put(wallet: str, raw: bytes, content_type: str, filename: str = 'file') -> dict:
    """
    Store media bytes; return {url, backend, id?}.
    local → caller persists via chat-image/chat-file (legacy SQLite).
    fs    → files under DATA_DIR/media; url=/media/...
    s3    → R2/S3 public URL (preferred for lightchain.ai).
    """
    wallet = (wallet or '').lower().strip()
    content_type = content_type or 'application/octet-stream'
    filename = (filename or 'file').replace('..', '_').replace('/', '_')
    if MEDIA_BACKEND == 's3':
        try:
            return _media_store_put_s3(wallet, raw, content_type, filename)
        except Exception as e:
            print(f'  [media] s3 put failed, caller may fall back: {e}')
            raise
    if MEDIA_BACKEND == 'fs':
        return _media_store_put_fs(wallet, raw, content_type, filename)
    return {
        'backend': 'local',
        'url': None,  # local path uses DB ids; see chat-image / chat-file routes
        'bytes': len(raw or b''),
        'content_type': content_type,
        'filename': filename,
    }


def _media_key(wallet: str, filename: str) -> str:
    import uuid as _uuid
    ext = ''
    if '.' in filename:
        ext = '.' + filename.rsplit('.', 1)[-1][:8].lower()
    safe_w = re.sub(r'[^a-f0-9]', '', (wallet or 'anon')[:42])[:16] or 'anon'
    return f"{safe_w}/{int(time.time())}_{_uuid.uuid4().hex[:12]}{ext}"


def _media_store_put_fs(wallet: str, raw: bytes, content_type: str, filename: str) -> dict:
    """Store on local disk under MEDIA_FS_DIR (good Railway volume / Compose default)."""
    os.makedirs(MEDIA_FS_DIR, exist_ok=True)
    key = _media_key(wallet, filename)
    abs_path = os.path.join(MEDIA_FS_DIR, key)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, 'wb') as f:
        f.write(raw or b'')
    return {
        'backend': 'fs',
        'url': f'/media/{key}',
        'key': key,
        'bytes': len(raw or b''),
        'content_type': content_type,
        'filename': filename,
    }


def _media_store_put_s3(wallet: str, raw: bytes, content_type: str, filename: str) -> dict:
    """S3/R2-compatible put via boto3."""
    endpoint = (os.environ.get('MEDIA_S3_ENDPOINT') or '').strip()
    bucket = (os.environ.get('MEDIA_S3_BUCKET') or '').strip()
    key_id = (os.environ.get('MEDIA_S3_ACCESS_KEY') or '').strip()
    secret = (os.environ.get('MEDIA_S3_SECRET_KEY') or '').strip()
    public_base = (os.environ.get('MEDIA_S3_PUBLIC_BASE') or '').rstrip('/')
    region = (os.environ.get('MEDIA_S3_REGION') or 'auto').strip() or 'auto'
    if not (bucket and key_id and secret and public_base):
        raise RuntimeError('MEDIA_S3_* env incomplete (need BUCKET, ACCESS_KEY, SECRET_KEY, PUBLIC_BASE)')
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as e:
        raise RuntimeError('boto3 not installed — pip install boto3 for MEDIA_BACKEND=s3') from e
    key = f"lightchat/{_media_key(wallet, filename)}"
    client_kwargs = {
        'aws_access_key_id': key_id,
        'aws_secret_access_key': secret,
        'region_name': region,
        'config': BotoConfig(signature_version='s3v4'),
    }
    if endpoint:
        client_kwargs['endpoint_url'] = endpoint
    s3 = boto3.client('s3', **client_kwargs)
    extra = {'ContentType': content_type}
    # R2 often ignores ACL; public access via PUBLIC_BASE / bucket settings
    s3.put_object(Bucket=bucket, Key=key, Body=raw, **extra)
    url = f"{public_base}/{key}"
    return {
        'backend': 's3',
        'url': url,
        'key': key,
        'bytes': len(raw or b''),
        'content_type': content_type,
        'filename': filename,
    }

def get_room(w1, w2):
    return '_'.join(sorted([w1.lower(), w2.lower()]))

def _strip_at_handle(h):
    return (h or '').lstrip('@')


def migrate_strip_at_handles(conn=None):
    """Rewrite legacy @handle rows to handle without @ (idempotent)."""
    close = conn is None
    if close:
        conn = get_db()
    try:
        rows = conn.execute("SELECT wallet, handle FROM handles WHERE handle LIKE '@%'").fetchall()
        for r in rows:
            clean = _strip_at_handle(r['handle']).lower()
            if not clean:
                continue
            # Avoid unique conflicts if both @x and x exist
            other = conn.execute(
                'SELECT wallet FROM handles WHERE handle = ? AND wallet != ?',
                (clean, r['wallet']),
            ).fetchone()
            if other:
                continue
            conn.execute('UPDATE handles SET handle = ? WHERE wallet = ?', (clean, r['wallet']))
        conn.commit()
    except Exception as e:
        print(f'  [handles] strip @ migrate: {e}')
    finally:
        if close:
            conn.close()


def get_handle_for(wallet, conn=None):
    close = conn is None
    if close:
        conn = get_db()
    row = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (wallet.lower(),)).fetchone()
    if close:
        conn.close()
    if row and row['handle']:
        return _strip_at_handle(row['handle'])
    return wallet[:8] + '...'

def send_push_notification(to_wallet, title, body, extra_data=None):
    if not WEB_PUSH_ENABLED:
        return
    try:
        conn = get_db()
        rows = conn.execute(
            'SELECT subscription FROM push_subscriptions WHERE wallet = ?',
            (to_wallet.lower(),)
        ).fetchall()
        conn.close()
        payload_dict = {'title': title, 'body': body}
        if extra_data:
            payload_dict['data'] = extra_data
        payload = json.dumps(payload_dict)
        for row in rows:
            try:
                sub = json.loads(row['subscription'])
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS
                )
            except WebPushException as e:
                # 410 Gone = subscription expired, remove it
                if '410' in str(e):
                    try:
                        sub_data = json.loads(row['subscription'])
                        endpoint = sub_data.get('endpoint', '')
                        conn2 = get_db()
                        conn2.execute(
                            'DELETE FROM push_subscriptions WHERE wallet = ? AND endpoint = ?',
                            (to_wallet.lower(), endpoint)
                        )
                        conn2.commit()
                        conn2.close()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# SESSION AUTH — sign-once (hard). Tokens live in SQLite so Railway
# restarts / refreshes keep the user signed in (localStorage Bearer).
# ══════════════════════════════════════════════════════════════════════
# sid -> wallet (socket session)
# Hot cache: token -> {wallet, exp, mode} (DB is source of truth)
_socket_wallets = {}
_auth_sessions = {}
_auth_nonces = {}  # wallet -> {nonce, exp}
_AUTH_SESSION_TTL = 30 * 24 * 3600  # 30 days
_AUTH_NONCE_TTL = 10 * 60
_auth_lock = threading.Lock()


def _new_session_token():
    return secrets.token_urlsafe(32)


def _auth_sessions_purge_expired(conn=None):
    """Delete expired rows. Own connection if none passed."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        now = int(time.time())
        conn.execute('DELETE FROM auth_sessions WHERE exp < ?', (now,))
        if own:
            conn.commit()
    except Exception as e:
        print(f'  [auth] purge failed: {e}', flush=True)
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _store_session(wallet, mode):
    token = _new_session_token()
    now = int(time.time())
    exp = now + _AUTH_SESSION_TTL
    wallet = (wallet or '').lower().strip()
    mode = (mode or 'signed').strip() or 'signed'
    conn = get_db()
    try:
        _auth_sessions_purge_expired(conn)
        conn.execute(
            '''INSERT INTO auth_sessions (token, wallet, mode, exp, created_at)
               VALUES (?,?,?,?,?)''',
            (token, wallet, mode, exp, now),
        )
        conn.commit()
    finally:
        conn.close()
    with _auth_lock:
        _auth_sessions[token] = {'wallet': wallet, 'exp': exp, 'mode': mode}
    return token, exp


def _lookup_session(token):
    if not token:
        return None
    now = int(time.time())
    with _auth_lock:
        row = _auth_sessions.get(token)
        if row:
            if row.get('exp', 0) < now:
                del _auth_sessions[token]
            else:
                return row
    conn = get_db()
    try:
        r = conn.execute(
            'SELECT wallet, mode, exp FROM auth_sessions WHERE token=?',
            (token,),
        ).fetchone()
        if not r:
            return None
        if int(r['exp'] or 0) < now:
            conn.execute('DELETE FROM auth_sessions WHERE token=?', (token,))
            conn.commit()
            return None
        out = {
            'wallet': (r['wallet'] or '').lower(),
            'exp': int(r['exp'] or 0),
            'mode': r['mode'] or 'signed',
        }
        with _auth_lock:
            _auth_sessions[token] = out
        return out
    finally:
        conn.close()


def _revoke_session(token):
    if not token:
        return False
    gone = False
    with _auth_lock:
        if token in _auth_sessions:
            del _auth_sessions[token]
            gone = True
    conn = get_db()
    try:
        cur = conn.execute('DELETE FROM auth_sessions WHERE token=?', (token,))
        conn.commit()
        if cur.rowcount:
            gone = True
    finally:
        conn.close()
    return gone


def _wallet_for_sid(sid):
    return _socket_wallets.get(sid)


@app.route('/api/auth/challenge', methods=['GET', 'POST'])
def api_auth_challenge():
    if request.method == 'POST':
        data = request.json or {}
        wallet = (data.get('wallet') or data.get('address') or '').lower().strip()
    else:
        wallet = (request.args.get('address') or request.args.get('wallet') or '').lower().strip()
    if not re.match(r'^0x[0-9a-f]{40}$', wallet):
        return jsonify({'error': 'valid wallet required'}), 400
    nonce = secrets.token_hex(16)
    exp = int(time.time()) + _AUTH_NONCE_TTL
    with _auth_lock:
        _auth_nonces[wallet] = {'nonce': nonce, 'exp': exp}
    # personal_sign friendly message (human readable)
    message = (
        f"LightChat login\n"
        f"Wallet: {wallet}\n"
        f"Nonce: {nonce}\n"
        f"Chain ID: {_AIVM_CHAIN_ID}\n"
        f"Issued at: {int(time.time())}"
    )
    return jsonify({'message': message, 'nonce': nonce, 'wallet': wallet})


@app.route('/api/auth/verify', methods=['POST'])
def api_auth_verify():
    """Verify personal_sign and issue a session token (hard auth)."""
    data = request.json or {}
    wallet = (data.get('wallet') or '').lower().strip()
    message = data.get('message') or ''
    signature = (data.get('signature') or '').strip()
    if not re.match(r'^0x[0-9a-f]{40}$', wallet) or not message or not signature:
        return jsonify({'error': 'wallet, message, and signature required'}), 400
    with _auth_lock:
        nonce_row = _auth_nonces.get(wallet)
    if not nonce_row or nonce_row.get('exp', 0) < int(time.time()):
        return jsonify({'error': 'challenge expired — request a new one'}), 400
    if nonce_row['nonce'] not in message:
        return jsonify({'error': 'message does not match challenge'}), 400
    if wallet not in message.lower():
        return jsonify({'error': 'message wallet mismatch'}), 400
    # Require Lightchain chain id in the signed message
    if f"Chain ID: {_AIVM_CHAIN_ID}" not in message:
        return jsonify({'error': f'Lightchain network required (chain {_AIVM_CHAIN_ID})'}), 400
    client_chain = data.get('chainId')
    if client_chain is not None:
        try:
            if int(client_chain) != _AIVM_CHAIN_ID:
                return jsonify({'error': f'Wrong network — use Lightchain (chain {_AIVM_CHAIN_ID})'}), 400
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid chainId'}), 400
    try:
        from eth_account.messages import encode_defunct
        from eth_account import Account
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        if recovered.lower() != wallet:
            return jsonify({'error': 'signature does not match wallet'}), 401
    except Exception as e:
        return jsonify({'error': 'invalid signature: ' + str(e)}), 401
    with _auth_lock:
        _auth_nonces.pop(wallet, None)
    token, exp = _store_session(wallet, 'signed')
    return jsonify({
        'token': token,
        'expires_at': exp,
        'wallet': wallet,
        'mode': 'signed'
    })


@app.route('/api/auth/soft', methods=['POST'])
def api_auth_soft():
    """Disabled — LightChat requires a wallet on Lightchain network (signed login)."""
    return jsonify({
        'error': 'Soft login disabled. Connect a wallet on Lightchain (chain 9200) and sign in.',
        'code': 'lc_network_required',
        'chainId': _AIVM_CHAIN_ID,
    }), 403


@app.route('/api/auth/session', methods=['GET'])
def api_auth_session():
    """Validate Bearer token."""
    auth = request.headers.get('Authorization') or ''
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else (request.args.get('token') or '')
    row = _lookup_session(token)
    if not row:
        return jsonify({'valid': False}), 401
    return jsonify({
        'valid': True,
        'wallet': row['wallet'],
        'mode': row.get('mode'),
        'expires_at': row.get('exp')
    })


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """Revoke a session token (Log off)."""
    data = request.json or {}
    auth = request.headers.get('Authorization') or ''
    token = (data.get('token') or '').strip()
    if not token and auth.lower().startswith('bearer '):
        token = auth[7:].strip()
    if not token:
        return jsonify({'ok': True, 'revoked': False})
    gone = _revoke_session(token)
    return jsonify({'ok': True, 'revoked': gone})


_HANDOFF_TTL = 180  # 3 minutes


def _auth_handoff_purge(conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        now = int(time.time())
        conn.execute('DELETE FROM auth_handoff_codes WHERE exp < ? OR used != 0', (now,))
        if own:
            conn.commit()
    except Exception as e:
        print(f'  [auth] handoff purge failed: {e}', flush=True)
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/auth/handoff/create', methods=['POST'])
def api_auth_handoff_create():
    """Desktop (Bearer) mints a 3-min single-use code for the same wallet."""
    auth = request.headers.get('Authorization') or ''
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    row = _lookup_session(token)
    if not row:
        return jsonify({'error': 'unauthorized', 'code': 'auth_required'}), 401
    wallet = (row.get('wallet') or '').lower()
    mode = row.get('mode') or 'signed'
    if not re.match(r'^0x[0-9a-f]{40}$', wallet):
        return jsonify({'error': 'invalid session wallet'}), 401
    now = int(time.time())
    exp = now + _HANDOFF_TTL
    code = secrets.token_urlsafe(9)
    conn = get_db()
    try:
        _auth_handoff_purge(conn)
        # Only newest unused code per wallet stays live
        conn.execute(
            'DELETE FROM auth_handoff_codes WHERE wallet=? AND used=0',
            (wallet,),
        )
        conn.execute(
            '''INSERT INTO auth_handoff_codes (code, wallet, mode, exp, used, created_at)
               VALUES (?,?,?,?,0,?)''',
            (code, wallet, mode, exp, now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'code': code, 'expires_at': exp})


@app.route('/api/auth/handoff/redeem', methods=['POST'])
def api_auth_handoff_redeem():
    """Mobile redeems a one-time code → normal session (same shape as /verify)."""
    data = request.json or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'code required'}), 400
    now = int(time.time())
    conn = get_db()
    try:
        _auth_handoff_purge(conn)
        row = conn.execute(
            'SELECT code, wallet, mode, exp, used FROM auth_handoff_codes WHERE code=?',
            (code,),
        ).fetchone()
        if not row or int(row['used'] or 0) != 0 or int(row['exp'] or 0) < now:
            return jsonify({'error': 'code expired or already used'}), 400
        # Mark used BEFORE minting (single-use under race)
        cur = conn.execute(
            'UPDATE auth_handoff_codes SET used=1 WHERE code=? AND used=0 AND exp>=?',
            (code, now),
        )
        conn.commit()
        if cur.rowcount != 1:
            return jsonify({'error': 'code expired or already used'}), 400
        wallet = (row['wallet'] or '').lower()
        mode = row['mode'] or 'signed'
    finally:
        conn.close()
    token, exp = _store_session(wallet, mode)
    return jsonify({
        'token': token,
        'expires_at': exp,
        'wallet': wallet,
        'mode': mode,
    })


@app.route('/health')
def health():
    db_info = {
        'path': DB_PATH,
        'data_dir': _data_dir,
        'exists': os.path.exists(DB_PATH),
        'bytes': 0,
        'garden_waters': None,
        'garden_events': None,
    }
    try:
        if db_info['exists']:
            db_info['bytes'] = os.path.getsize(DB_PATH)
        conn = get_db()
        try:
            g = conn.execute(
                "SELECT waters FROM community_garden WHERE community_id=?",
                ('lightchain-official',),
            ).fetchone()
            db_info['garden_waters'] = int(g['waters']) if g else 0
            try:
                ev = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(MAX(size),0) AS mx FROM community_garden_events WHERE community_id=?",
                    ('lightchain-official',),
                ).fetchone()
                db_info['garden_events'] = int(ev['n'] or 0) if ev else 0
                db_info['garden_event_max'] = int(ev['mx'] or 0) if ev else 0
            except Exception:
                db_info['garden_events'] = 0
        finally:
            conn.close()
    except Exception as e:
        db_info['error'] = str(e)[:120]
    turn_key = bool(os.environ.get('METERED_API_KEY', '').strip())
    try:
        from community import knowledge_sources_from_env
        _knowledge_n = len(knowledge_sources_from_env())
    except Exception:
        _knowledge_n = 0
    return jsonify({
        'status': 'ok',
        'service': 'LightChat',
        'aivm_protocol': _AIVM_PROTOCOL,
        'aivm_gateway': _AIVM_GATEWAY,
        'aivm_ready': bool(os.environ.get('LIGHTCHAIN_PRIVATE_KEY', '').strip()),
        'job_registry': _AIVM_JOB_REG,
        'knowledge_sources': _knowledge_n,
        'dao_governor': (os.environ.get('DAO_GOVERNOR_ADDRESS') or _DAO_GOVERNOR_DEFAULT),
        'auth': 'session-v1',
        'db': db_info,
        'media': media_backend_status(),
        'turn': {
            'metered_configured': turn_key,
            'endpoint': '/api/turn-credentials',
        },
    })


@app.route('/media/<path:key>')
def serve_media_fs(key):
    """Serve MEDIA_BACKEND=fs objects from DATA_DIR/media."""
    # Prevent path escape
    key = (key or '').replace('..', '').lstrip('/')
    if not key:
        return jsonify({'error': 'not found'}), 404
    abs_path = os.path.abspath(os.path.join(MEDIA_FS_DIR, key))
    root = os.path.abspath(MEDIA_FS_DIR)
    if not abs_path.startswith(root + os.sep) and abs_path != root:
        return jsonify({'error': 'forbidden'}), 403
    if not os.path.isfile(abs_path):
        return jsonify({'error': 'not found'}), 404
    # Guess type from extension
    import mimetypes
    ctype = mimetypes.guess_type(abs_path)[0] or 'application/octet-stream'
    return send_from_directory(os.path.dirname(abs_path), os.path.basename(abs_path), mimetype=ctype)


STICKERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stickers')

@app.route('/stickers/<path:filename>')
def serve_sticker(filename):
    return send_from_directory(STICKERS_DIR, filename)

@app.route('/vapid-public-key')
def vapid_public_key():
    if not WEB_PUSH_ENABLED:
        return jsonify({'error': 'web push not configured — set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY'}), 503
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})

@app.route('/subscribe', methods=['POST'])
def subscribe():
    if not WEB_PUSH_ENABLED:
        return jsonify({'error': 'web push not configured'}), 503
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    subscription = data.get('subscription')
    if not wallet or not subscription:
        return jsonify({'error': 'wallet and subscription required'}), 400
    endpoint = subscription.get('endpoint', '')
    if not endpoint:
        return jsonify({'error': 'invalid subscription'}), 400
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO push_subscriptions (wallet, endpoint, subscription, created_at) VALUES (?, ?, ?, ?)',
        (wallet, endpoint, json.dumps(subscription), int(time.time()))
    )
    conn.commit()
    conn.close()
    return jsonify({'subscribed': True})

@app.route('/unsubscribe', methods=['POST'])
def unsubscribe():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    endpoint = data.get('endpoint', '')
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    conn = get_db()
    if endpoint:
        conn.execute(
            'DELETE FROM push_subscriptions WHERE wallet = ? AND endpoint = ?',
            (wallet, endpoint)
        )
    else:
        conn.execute('DELETE FROM push_subscriptions WHERE wallet = ?', (wallet,))
    conn.commit()
    conn.close()
    return jsonify({'unsubscribed': True})

@app.route('/register', methods=['POST'])
def register():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    handle = data.get('handle', '').strip().lstrip('@')

    if not wallet or not handle:
        return jsonify({'error': 'wallet and handle required'}), 400

    if not re.match(r'^[a-zA-Z0-9_]{2,20}$', handle):
        return jsonify({'error': 'Handle must be 2-20 characters: letters, numbers, underscores only'}), 400

    handle = handle.lower()  # no @ symbol
    conn = get_db()
    try:
        existing = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (wallet,)).fetchone()
        if existing:
            clean = (existing['handle'] or '').lstrip('@')
            return jsonify({'handle': clean, 'exists': True})

        conn.execute('INSERT INTO handles (wallet, handle, created_at) VALUES (?, ?, ?)',
                     (wallet, handle, int(time.time())))
        conn.commit()
        return jsonify({'handle': handle, 'registered': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Handle already taken, please choose another'}), 409
    finally:
        conn.close()

@app.route('/handle/<wallet>')
def get_handle(wallet):
    conn = get_db()
    row = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (wallet.lower(),)).fetchone()
    conn.close()
    if not row or not row['handle']:
        return jsonify({'handle': None})
    return jsonify({'handle': (row['handle'] or '').lstrip('@')})

@app.route('/resolve/<handle>')
def resolve_handle(handle):
    raw = (handle or '').lstrip('@').lower()
    conn = get_db()
    # Accept legacy @handle rows and new handle-without-@
    row = conn.execute(
        'SELECT wallet FROM handles WHERE handle = ? OR handle = ?',
        (raw, '@' + raw),
    ).fetchone()
    conn.close()
    if row:
        return jsonify({'wallet': row['wallet']})
    return jsonify({'wallet': None}), 404

# Anti-scam: limit how many friend requests one wallet can send
_FRIEND_REQ_LIMIT = 8          # max pending/sent requests
_FRIEND_REQ_WINDOW_SEC = 3600  # per rolling hour


@app.route('/contact-request', methods=['POST'])
def contact_request():
    data = request.json or {}
    wallet = data.get('wallet', '').lower()
    contact_wallet = data.get('contact_wallet', '').lower()

    if not wallet or not contact_wallet:
        return jsonify({'error': 'wallet and contact_wallet required'}), 400
    if wallet == contact_wallet:
        return jsonify({'error': 'Cannot add yourself'}), 400

    conn = get_db()
    try:
        # Check if already approved in either direction
        approved = conn.execute(
            'SELECT status FROM contacts WHERE wallet = ? AND contact_wallet = ? AND status = ?',
            (wallet, contact_wallet, 'approved')
        ).fetchone()
        if approved:
            return jsonify({'status': 'already_contacts'})

        # Rate limit: how many requests this wallet sent recently
        # Pending rows are stored as (recipient=contact_wallet, sender=wallet) in contact_wallet column
        since = int(time.time()) - _FRIEND_REQ_WINDOW_SEC
        recent = conn.execute(
            '''SELECT COUNT(*) AS c FROM contacts
               WHERE contact_wallet = ? AND status = ? AND created_at >= ?''',
            (wallet, 'pending', since),
        ).fetchone()['c']
        if recent >= _FRIEND_REQ_LIMIT:
            return jsonify({
                'error': f'Too many friend requests — try again later (max {_FRIEND_REQ_LIMIT}/hour)',
                'code': 'rate_limited',
            }), 429

        # Insert pending request: contact_wallet receives the request
        conn.execute(
            'INSERT OR IGNORE INTO contacts (wallet, contact_wallet, status, created_at) VALUES (?, ?, ?, ?)',
            (contact_wallet, wallet, 'pending', int(time.time()))
        )
        conn.commit()

        # Notify recipient if online (socket toast)
        from_handle = get_handle_for(wallet, conn)
        socketio.emit('contact_request', {
            'from_wallet': wallet,
            'handle': from_handle
        }, room=contact_wallet)

        # Web Push when recipient has notifications enabled (app backgrounded)
        eventlet.spawn(
            send_push_notification,
            contact_wallet,
            '➕ New contact request',
            (from_handle or (wallet[:8] + '…')) + ' wants to add you on LightChat',
            {
                'type': 'contact_request',
                'from_wallet': wallet,
                'from_handle': from_handle,
                'url': 'https://lightchat.chat/?tab=contacts',
            },
        )

        return jsonify({'status': 'sent'})
    finally:
        conn.close()

@app.route('/approve-contact', methods=['POST'])
def approve_contact():
    data = request.json or {}
    wallet = data.get('wallet', '').lower()
    contact_wallet = data.get('contact_wallet', '').lower()

    conn = get_db()
    try:
        conn.execute(
            'UPDATE contacts SET status = ? WHERE wallet = ? AND contact_wallet = ?',
            ('approved', wallet, contact_wallet)
        )
        # Add reverse approved relationship
        conn.execute(
            'INSERT OR REPLACE INTO contacts (wallet, contact_wallet, status, created_at) VALUES (?, ?, ?, ?)',
            (contact_wallet, wallet, 'approved', int(time.time()))
        )
        conn.commit()

        # Notify both parties
        socketio.emit('contact_approved', {
            'wallet': contact_wallet,
            'handle': get_handle_for(contact_wallet)
        }, room=wallet)
        socketio.emit('contact_approved', {
            'wallet': wallet,
            'handle': get_handle_for(wallet)
        }, room=contact_wallet)

        return jsonify({'status': 'approved'})
    finally:
        conn.close()

@app.route('/delete-handle', methods=['POST'])
def delete_handle():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    conn = get_db()
    conn.execute('DELETE FROM handles WHERE wallet = ?', (wallet,))
    conn.commit()
    conn.close()
    return jsonify({'deleted': True})

@app.route('/change-handle', methods=['POST'])
def change_handle():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    handle = data.get('handle', '').strip().lstrip('@')

    if not wallet or not handle:
        return jsonify({'error': 'wallet and handle required'}), 400
    if not re.match(r'^[a-zA-Z0-9_]{2,20}$', handle):
        return jsonify({'error': 'Handle must be 2-20 characters: letters, numbers, underscores only'}), 400

    handle = handle.lower()  # no @ symbol
    conn = get_db()
    try:
        existing = conn.execute(
            'SELECT wallet FROM handles WHERE handle = ? OR handle = ?',
            (handle, '@' + handle),
        ).fetchone()
        if existing and existing['wallet'] != wallet:
            return jsonify({'error': 'Handle already taken, please choose another'}), 409
        conn.execute('INSERT OR REPLACE INTO handles (wallet, handle, created_at) VALUES (?, ?, ?)',
                     (wallet, handle, int(time.time())))
        conn.commit()
        return jsonify({'handle': handle, 'updated': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Handle already taken, please choose another'}), 409
    finally:
        conn.close()

@app.route('/delete-contact', methods=['POST'])
def delete_contact():
    data = request.json or {}
    wallet = data.get('wallet', '').lower()
    contact_wallet = data.get('contact_wallet', '').lower()
    if not wallet or not contact_wallet:
        return jsonify({'error': 'wallet and contact_wallet required'}), 400
    conn = get_db()
    conn.execute(
        'DELETE FROM contacts WHERE (wallet = ? AND contact_wallet = ?) OR (wallet = ? AND contact_wallet = ?)',
        (wallet, contact_wallet, contact_wallet, wallet)
    )
    conn.commit()
    conn.close()
    return jsonify({'deleted': True})

@app.route('/contacts/<wallet>')
def get_contacts(wallet):
    conn = get_db()
    rows = conn.execute(
        '''SELECT c.contact_wallet as wallet, c.status, h.handle
           FROM contacts c
           LEFT JOIN handles h ON h.wallet = c.contact_wallet
           WHERE c.wallet = ?
           ORDER BY c.created_at DESC''',
        (wallet.lower(),)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/post-memory', methods=['POST'])
def post_memory():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    caption = data.get('caption', '').strip()
    image_data = data.get('image_data', '')
    image_type = data.get('image_type', '')
    storage_type = data.get('storage_type', 'cloud')
    chain_memory_id = data.get('chain_memory_id', None)

    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    if not caption and not image_data:
        return jsonify({'error': 'caption or image required'}), 400

    now = int(time.time())
    expires_at = now + (90 * 24 * 60 * 60) if storage_type == 'cloud' else None

    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO memories (wallet, caption, image_data, image_type, storage_type, chain_memory_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (wallet, caption, image_data or None, image_type or None, storage_type, chain_memory_id, now, expires_at)
    )
    memory_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': memory_id, 'posted': True})

@app.route('/memories/<wallet>')
def get_memories(wallet):
    w = wallet.lower()
    now = int(time.time())
    conn = get_db()
    rows = conn.execute('''
        SELECT m.id, m.wallet, m.caption, m.image_data, m.image_type,
               m.storage_type, m.created_at, m.expires_at, h.handle,
               COALESCE(m.media_type, 'image') as media_type,
               m.chain_memory_id
        FROM memories m
        LEFT JOIN handles h ON h.wallet = m.wallet
        WHERE m.wallet IN (
            SELECT contact_wallet FROM contacts WHERE wallet = ? AND status = 'approved'
            UNION SELECT ?
        )
        AND (m.expires_at IS NULL OR m.expires_at > ?)
        ORDER BY m.created_at DESC
        LIMIT 50
    ''', (w, w, now)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/chat-image', methods=['POST'])
def post_chat_image():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    image_data = data.get('image_data', '')
    image_type = data.get('image_type', 'image/jpeg')
    if not wallet or not image_data:
        return jsonify({'error': 'wallet and image_data required'}), 400
    # Prefer fs/s3 when configured (lightchain.ai handoff path)
    if MEDIA_BACKEND in ('s3', 'fs'):
        try:
            raw = base64.b64decode(image_data)
            put = media_store_put(wallet, raw, image_type, 'image.jpg')
            if put.get('url'):
                return jsonify({'url': put['url'], 'backend': put.get('backend') or MEDIA_BACKEND, 'image_id': None})
        except Exception as e:
            print(f'  [chat-image] {MEDIA_BACKEND} failed, falling back to sqlite: {e}')
    now = int(time.time())
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO chat_images (wallet, image_data, image_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
        (wallet, image_data, image_type, now, NEVER_EXPIRES)
    )
    image_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'image_id': image_id, 'backend': 'local'})

@app.route('/chat-image/<int:image_id>')
def get_chat_image(image_id):
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        'SELECT image_data, image_type FROM chat_images WHERE id = ? AND expires_at > ?',
        (image_id, now)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    img_bytes = base64.b64decode(row['image_data'])
    resp = make_response(img_bytes)
    resp.headers['Content-Type'] = row['image_type']
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/chat-image/<int:image_id>/download')
def download_chat_image(image_id):
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        'SELECT image_data, image_type FROM chat_images WHERE id = ? AND expires_at > ?',
        (image_id, now)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    img_bytes = base64.b64decode(row['image_data'])
    resp = make_response(img_bytes)
    resp.headers['Content-Type'] = row['image_type']
    resp.headers['Content-Disposition'] = 'attachment; filename="photo.jpg"'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

import urllib.request as _urllib_req
from urllib.parse import urlparse as _urlparse, quote as _url_quote

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

def _tenor_host_ok(url: str) -> bool:
    try:
        host = (_urlparse(url).hostname or '').lower()
    except Exception:
        return False
    return bool(
        host.endswith('tenor.com')
        or host.endswith('tenor.co')
        or host.endswith('googleapis.com')
        or 'tenor' in host
    )


@app.route('/proxy-gif')
def proxy_gif():
    url = request.args.get('url', '')
    name = request.args.get('name', 'image.gif')
    if not url:
        return jsonify({'error': 'no url'}), 400
    if not _tenor_host_ok(url):
        return jsonify({'error': 'domain not allowed'}), 403
    try:
        req = _urllib_req.Request(url, headers={'User-Agent': 'Mozilla/5.0 LightChat/1.0'})
        with _urllib_req.urlopen(req, timeout=12) as r:
            data = r.read()
            content_type = r.headers.get('Content-Type', 'image/gif')
        resp = make_response(data)
        resp.headers['Content-Type'] = content_type
        safe_name = name.replace('"', '\\"')
        resp.headers['Content-Disposition'] = f'inline; filename="{safe_name}"'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# GIF search stays off-server. Tiny resolve only: tenor.com/view/… → media .gif URL
# so paste-from-browser “Copy link” works. Message body remains URL-only (P2P-friendly).

@app.route('/api/gif-resolve')
def api_gif_resolve():
    """Resolve Tenor/Giphy page links to a direct media HTTPS URL."""
    raw = (request.args.get('url') or '').strip()
    if not raw or not raw.lower().startswith('https://'):
        return jsonify({'error': 'https url required'}), 400
    try:
        host = (_urlparse(raw).hostname or '').lower()
    except Exception:
        return jsonify({'error': 'bad url'}), 400
    allowed = (
        host.endswith('tenor.com')
        or host.endswith('tenor.co')
        or host.endswith('giphy.com')
        or host == 'gph.is'
    )
    if not allowed:
        return jsonify({'error': 'only Tenor/Giphy links'}), 403

    # Already a media URL
    low = raw.lower()
    if any(x in low for x in ('media.tenor.', 'media1.tenor.', 'c.tenor.', 'media.giphy.', 'i.giphy.', '.gif')):
        if '/view/' not in low and 'giphy.com/gifs/' not in low:
            return jsonify({'url': raw, 'resolved': False})

    media_url = None
    try:
        req = _urllib_req.Request(
            raw,
            headers={'User-Agent': 'Mozilla/5.0 LightChat/1.0', 'Accept': 'text/html'},
        )
        with _urllib_req.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
        import re as _re
        # Prefer direct gif
        patterns = [
            r'og:image"\s+content="(https://media1\.tenor\.com/[^"]+\.gif)"',
            r'og:image"\s+content="(https://media\.tenor\.com/[^"]+\.gif)"',
            r'og:image:secure_url"\s+content="(https://[^"]+\.gif)"',
            r'og:image"\s+content="(https://media[0-9]*\.giphy\.com/[^"]+)"',
            r'og:image"\s+content="(https://i\.giphy\.com/[^"]+)"',
            r'(https://media1\.tenor\.com/m/[^"\s]+\.gif)',
            r'(https://media\.tenor\.com/[^"\s]+\.gif)',
        ]
        for pat in patterns:
            m = _re.search(pat, html, _re.I)
            if m:
                media_url = m.group(1)
                break
        # Last resort: oembed thumbnail (may be png)
        if not media_url:
            oe = (
                'https://tenor.com/oembed?url='
                + _url_quote(raw)
            )
            req2 = _urllib_req.Request(oe, headers={'User-Agent': 'LightChat/1.0'})
            with _urllib_req.urlopen(req2, timeout=10) as resp2:
                data = json.loads(resp2.read().decode('utf-8', errors='ignore') or '{}')
            thumb = data.get('thumbnail_url') or ''
            if thumb:
                # Prefer .gif sibling of thumbnail .png when possible
                media_url = thumb.replace('.png', '.gif') if thumb.endswith('.png') else thumb
    except Exception as e:
        return jsonify({'error': str(e), 'url': None}), 502

    if not media_url:
        return jsonify({'error': 'could not find GIF media in that link', 'url': None}), 404
    return jsonify({'url': media_url, 'resolved': True})


@app.route('/chat-file', methods=['POST'])
def post_chat_file():
    """Accept JSON (legacy base64) or multipart FormData (preferred for video)."""
    wallet = ''
    file_name = 'file'
    file_type = 'application/octet-stream'
    file_size = 0
    file_data = ''  # base64 for DB storage

    ctype = (request.content_type or '').lower()
    if 'multipart/form-data' in ctype:
        wallet = (request.form.get('wallet') or '').lower().strip()
        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'file required'}), 400
        raw = f.read()
        file_name = f.filename or request.form.get('file_name') or 'file'
        file_type = f.mimetype or request.form.get('file_type') or 'application/octet-stream'
        file_size = len(raw)
        file_data = base64.b64encode(raw).decode('ascii')
    else:
        data = request.json or {}
        wallet = (data.get('wallet') or '').lower().strip()
        file_name = data.get('file_name', 'file')
        file_data = data.get('file_data', '')
        file_type = data.get('file_type', 'application/octet-stream')
        file_size = int(data.get('file_size', 0) or 0)

    if not wallet or not file_data:
        return jsonify({'error': 'wallet and file required'}), 400

    # Cap uploads — images smaller; video up to MEDIA_MAX_VIDEO_MB
    max_bytes = MEDIA_MAX_VIDEO_MB * 1024 * 1024
    if (file_type or '').startswith('image/'):
        max_bytes = MEDIA_MAX_IMAGE_MB * 1024 * 1024
    approx = file_size or int(len(file_data) * 0.75)
    if approx > max_bytes + 500_000:
        return jsonify({'error': f'File too large (max {max_bytes // (1024 * 1024)} MB)'}), 400

    raw = base64.b64decode(file_data)
    if MEDIA_BACKEND in ('s3', 'fs'):
        try:
            put = media_store_put(wallet, raw, file_type, file_name)
            if put.get('url'):
                return jsonify({
                    'url': put['url'],
                    'backend': put.get('backend') or MEDIA_BACKEND,
                    'file_type': file_type,
                    'file_name': file_name,
                    'file_id': None,
                })
        except Exception as e:
            print(f'  [chat-file] {MEDIA_BACKEND} failed, falling back to sqlite: {e}')

    now = int(time.time())
    try:
        conn = get_db()
        cursor = conn.execute(
            'INSERT INTO chat_files (wallet, file_name, file_data, file_type, file_size, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (wallet, file_name, file_data, file_type, approx, now, NEVER_EXPIRES)
        )
        file_id = cursor.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'  [chat-file] insert failed: {e}')
        return jsonify({'error': 'Could not save file — try a shorter clip or paste a YouTube link'}), 500
    return jsonify({'file_id': file_id, 'file_type': file_type, 'file_name': file_name, 'backend': 'local'})

@app.route('/chat-file/<int:file_id>')
def get_chat_file(file_id):
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        'SELECT file_name, file_data, file_type FROM chat_files WHERE id = ? AND expires_at > ?',
        (file_id, now)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    file_bytes = base64.b64decode(row['file_data'])
    resp = make_response(file_bytes)
    resp.headers['Content-Type'] = row['file_type']
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    safe_name = row['file_name'].replace('"', '\\"')
    # Images + videos play inline in chat; other files download
    ft = (row['file_type'] or '').lower()
    if ft.startswith('image/') or ft.startswith('video/'):
        resp.headers['Content-Disposition'] = f'inline; filename="{safe_name}"'
    else:
        resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return resp

@app.route('/chat-file/<int:file_id>/to-image', methods=['POST'])
def file_to_image(file_id):
    """Re-save a file as a chat image so it can be forwarded inline."""
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    conn = get_db()
    now = int(time.time())
    row = conn.execute(
        'SELECT file_data, file_type FROM chat_files WHERE id = ? AND expires_at > ?',
        (file_id, now)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    cursor = conn.execute(
        'INSERT INTO chat_images (wallet, image_data, image_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
        (wallet, row['file_data'], row['file_type'], now, NEVER_EXPIRES)
    )
    image_id = cursor.lastrowid
    conn.commit()
    conn.close()
    resp = jsonify({'image_id': image_id})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

def _voice_ext_for_type(content_type):
    ct = (content_type or 'audio/webm').split(';')[0].strip().lower()
    return {
        'audio/webm': '.webm',
        'audio/ogg': '.ogg',
        'audio/mp4': '.m4a',
        'audio/mpeg': '.mp3',
        'audio/mp3': '.mp3',
        'audio/x-m4a': '.m4a',
        'audio/aac': '.aac',
    }.get(ct, '.webm')


def transcode_voice_to_mp3(audio_data, content_type):
    """Convert any uploaded voice clip to MP3 for universal desktop/mobile playback."""
    if not audio_data or len(audio_data) < 100:
        return audio_data, (content_type or 'audio/webm').split(';')[0].strip()
    in_suffix = _voice_ext_for_type(content_type)
    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=in_suffix, delete=False) as inf:
            inf.write(audio_data)
            in_path = inf.name
        out_path = in_path + '.mp3'
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', in_path, '-vn', '-acodec', 'libmp3lame', '-q:a', '4', out_path],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
            return audio_data, (content_type or 'audio/webm').split(';')[0].strip()
        with open(out_path, 'rb') as outf:
            return outf.read(), 'audio/mpeg'
    except Exception:
        return audio_data, (content_type or 'audio/webm').split(';')[0].strip()
    finally:
        for path in (in_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _voice_response(audio_bytes, content_type):
    resp = make_response(audio_bytes)
    resp.headers['Content-Type'] = (content_type or 'audio/mpeg').split(';')[0].strip()
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/chat-voice', methods=['POST'])
def post_chat_voice():
    audio_data = request.data
    content_type = request.headers.get('Content-Type', 'audio/webm')
    if not audio_data:
        return jsonify({'error': 'no audio data'}), 400
    mp3_data, out_type = transcode_voice_to_mp3(audio_data, content_type)
    now = int(time.time())
    expires_at = NEVER_EXPIRES
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO chat_voices (voice_data, content_type, created_at, expires_at) VALUES (?, ?, ?, ?)',
        (base64.b64encode(mp3_data).decode('ascii'), out_type, now, expires_at)
    )
    voice_id = cursor.lastrowid
    conn.commit()
    conn.close()
    resp = jsonify({'url': '/voice/' + str(voice_id), 'mime': out_type})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/voice/<voice_id>')
def get_chat_voice(voice_id):
    now = int(time.time())
    if voice_id.isdigit():
        conn = get_db()
        row = conn.execute(
            'SELECT voice_data, content_type FROM chat_voices WHERE id = ? AND expires_at > ?',
            (int(voice_id), now)
        ).fetchone()
        conn.close()
        if row:
            audio_bytes = base64.b64decode(row['voice_data'])
            return _voice_response(audio_bytes, row['content_type'])
    with _voice_lock:
        entry = _voice_store.get(voice_id)
    if not entry or entry['expires_at'] < now:
        return jsonify({'error': 'not found'}), 404
    mp3_data, out_type = transcode_voice_to_mp3(entry['data'], entry['content_type'])
    return _voice_response(mp3_data, out_type)

# ── Chat video endpoints ──────────────────────────────────────────────

@app.route('/chat-video', methods=['POST'])
def post_chat_video():
    video_data = request.data
    content_type = request.headers.get('Content-Type', 'video/mp4')
    if not video_data:
        return jsonify({'error': 'no video data'}), 400
    if len(video_data) > 50 * 1024 * 1024:
        return jsonify({'error': 'Video too large — max 50 MB'}), 413
    video_id = str(uuid.uuid4())
    expires_at = int(time.time()) + 86400  # 24-hour TTL
    with _video_lock:
        _video_store[video_id] = {
            'data': video_data,
            'content_type': content_type,
            'expires_at': expires_at
        }
    return jsonify({'video_id': video_id})

@app.route('/video/<video_id>')
def get_video(video_id):
    with _video_lock:
        entry = _video_store.get(video_id)
    if not entry or entry['expires_at'] < int(time.time()):
        return jsonify({'error': 'not found'}), 404
    resp = make_response(entry['data'])
    resp.headers['Content-Type'] = entry['content_type']
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp

# ── Memory video endpoint ─────────────────────────────────────────────

@app.route('/post-memory-video', methods=['POST'])
def post_memory_video():
    video_data = request.data
    content_type = request.headers.get('Content-Type', 'video/mp4')
    wallet_h = request.headers.get('X-Wallet', '').lower().strip()
    caption_h = request.headers.get('X-Caption', '').strip()
    if not video_data:
        return jsonify({'error': 'video data required'}), 400
    if not wallet_h:
        return jsonify({'error': 'X-Wallet header required'}), 400
    if len(video_data) > 50 * 1024 * 1024:
        return jsonify({'error': 'Video too large — max 50 MB'}), 413

    video_id = str(uuid.uuid4())
    now = int(time.time())
    expires_at = now + (90 * 24 * 60 * 60)  # 90-day TTL to match cloud memories

    with _video_lock:
        _video_store[video_id] = {
            'data': video_data,
            'content_type': content_type,
            'expires_at': expires_at
        }

    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO memories (wallet, caption, image_data, image_type, media_type, storage_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (wallet_h, caption_h, video_id, content_type, 'video', 'cloud', now, expires_at)
    )
    memory_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': memory_id, 'video_id': video_id, 'posted': True})

@app.route('/api/groups/<wallet>')
def api_get_groups(wallet):
    w = wallet.lower().strip()
    now = int(time.time())
    conn = get_db()
    rows = conn.execute('''
        SELECT g.id, g.name, g.created_by, g.created_at,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) as member_count,
               (SELECT content FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_message,
               (SELECT type FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_type,
               (SELECT timestamp FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_time
        FROM groups g
        JOIN group_members mgr ON mgr.group_id = g.id AND mgr.wallet = ?
        ORDER BY COALESCE((SELECT timestamp FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1), 0) DESC
    ''', (now, now, now, w, now)).fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'name': r['name'], 'created_by': r['created_by'],
        'member_count': r['member_count'],
        'last_message': r['last_message'], 'last_type': r['last_type'],
        'last_time': r['last_time']
    } for r in rows])

@app.route('/messages/<wallet>/<contact_wallet>')
def get_messages(wallet, contact_wallet):
    room = get_room(wallet, contact_wallet)
    now = int(time.time())
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM messages WHERE room = ? AND expires_at > ? ORDER BY created_at ASC LIMIT 100',
        (room, now)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# In-memory voice message store: {uuid_str: {'data': bytes, 'content_type': str, 'expires_at': int}}
_voice_store = {}
_voice_lock = threading.Lock()

# In-memory video store (chat + memory videos): {uuid_str: {'data': bytes, 'content_type': str, 'expires_at': int}}
_video_store = {}
_video_lock = threading.Lock()

# Buffer for pending call offers — callee may be backgrounded/disconnected when call arrives
# {callee_wallet: {caller_wallet, handle, offer}}
pending_calls = {}

def _expire_pending_call(callee):
    """Auto-clear a pending call after 90 seconds."""
    eventlet.sleep(90)
    pending_calls.pop(callee, None)

# ── Group call state ──────────────────────────────────────────────────
# { group_id: { 'members': set(wallet), 'caller': wallet, 'group_name': str } }
group_calls = {}

def _expire_group_call(group_id, caller_wallet):
    """Auto-cancel a group call after 30 s if nobody joined."""
    eventlet.sleep(30)
    call = group_calls.get(group_id)
    if call and len(call['members']) == 1 and caller_wallet in call['members']:
        del group_calls[group_id]
        socketio.emit('group_call_ended', {'group_id': group_id, 'reason': 'timeout'}, room=caller_wallet)

# WebSocket handlers
@socketio.on('connect')
def on_connect():
    pass


# Channel / DM P2P — signaling only (SDP/ICE). Message bodies travel on WebRTC datachannels.
_genchat_p2p_peers = {}  # wallet -> sid (legacy / last channel)
_channel_p2p_peers = {}  # channel -> {wallet: sid}
_dm_p2p_peers = {}  # wallet -> sid


def _p2p_room(channel):
    ch = (channel or 'general').lower().strip() or 'general'
    return 'p2p:' + ch


@socketio.on('disconnect')
def on_disconnect():
    w = _socket_wallets.pop(request.sid, None)
    if w and _genchat_p2p_peers.get(w) == request.sid:
        _genchat_p2p_peers.pop(w, None)
    if w:
        for ch, peers in list(_channel_p2p_peers.items()):
            if peers.get(w) == request.sid:
                peers.pop(w, None)
                socketio.emit(
                    'genchat_p2p_peer_left',
                    {'wallet': w, 'channel': ch},
                    room=_p2p_room(ch),
                    skip_sid=request.sid,
                )
    if w and _dm_p2p_peers.get(w) == request.sid:
        _dm_p2p_peers.pop(w, None)
        socketio.emit('dm_p2p_peer_left', {'wallet': w}, skip_sid=request.sid)
    if w:
        try:
            from community import presence_on_disconnect
            presence_on_disconnect(w, request.sid, socketio)
        except Exception as e:
            print(f'  [presence] disconnect: {e}')


@socketio.on('auth')
def on_auth(data):
    data = data or {}
    wallet = (data.get('wallet') or '').lower()
    token = (data.get('token') or '').strip()
    row = _lookup_session(token)
    if not row or row['wallet'] != wallet:
        emit('auth_error', {
            'message': 'Session required — reconnect your wallet',
            'code': 'auth_required'
        })
        return
    _socket_wallets[request.sid] = wallet
    join_room(wallet)
    # Catch-all room so Gen Chat posts reach every logged-in device even if
    # channel join was missed after reconnect (client still filters by slug).
    join_room('community:all')
    emit('auth_ok', {'wallet': wallet, 'mode': row.get('mode', 'soft')})
    try:
        from community import presence_on_auth
        presence_on_auth(wallet, request.sid, get_db, socketio)
    except Exception as e:
        print(f'  [presence] auth: {e}')
    # Deliver any buffered call offer — handles iOS reconnect after being backgrounded
    pending = pending_calls.get(wallet)
    if pending:
        emit('call_offer', {
            'caller_wallet': pending['caller_wallet'],
            'handle': pending['handle'],
            'offer': pending['offer']
        })


@socketio.on('genchat_p2p_join')
def on_genchat_p2p_join(data):
    """Join a live-chat P2P signaling room (general, dev, …)."""
    data = data or {}
    wallet = (data.get('wallet') or '').lower()
    channel = (data.get('channel') or data.get('slug') or 'general').lower().strip() or 'general'
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != wallet:
        emit('error', {'message': 'Not authenticated'})
        return
    # Drop this wallet from other channel maps (one live channel mesh at a time)
    for ch, peers in list(_channel_p2p_peers.items()):
        if ch != channel and peers.get(wallet) == request.sid:
            peers.pop(wallet, None)
            leave_room(_p2p_room(ch))
            emit('genchat_p2p_peer_left', {'wallet': wallet, 'channel': ch}, room=_p2p_room(ch), include_self=False)
    room = _p2p_room(channel)
    join_room(room)
    # legacy room for older clients on general
    if channel == 'general':
        join_room('genchat_p2p')
    peers_map = _channel_p2p_peers.setdefault(channel, {})
    peers_map[wallet] = request.sid
    _genchat_p2p_peers[wallet] = request.sid
    peers = [w for w in peers_map.keys() if w != wallet]
    emit('genchat_p2p_peers', {'peers': peers, 'channel': channel})
    emit('genchat_p2p_peer_joined', {'wallet': wallet, 'channel': channel}, room=room, include_self=False)


@socketio.on('genchat_p2p_leave')
def on_genchat_p2p_leave(data):
    data = data or {}
    wallet = (data.get('wallet') or '').lower()
    channel = (data.get('channel') or data.get('slug') or 'general').lower().strip() or 'general'
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or (wallet and auth_w != wallet):
        return
    w = wallet or auth_w
    peers_map = _channel_p2p_peers.get(channel) or {}
    if peers_map.get(w) == request.sid:
        peers_map.pop(w, None)
    if _genchat_p2p_peers.get(w) == request.sid:
        _genchat_p2p_peers.pop(w, None)
    leave_room(_p2p_room(channel))
    if channel == 'general':
        leave_room('genchat_p2p')
    emit('genchat_p2p_peer_left', {'wallet': w, 'channel': channel}, room=_p2p_room(channel), include_self=False)


@socketio.on('dm_p2p_hello')
def on_dm_p2p_hello(data):
    """Register wallet for DM P2P signaling (not message bodies)."""
    data = data or {}
    wallet = (data.get('wallet') or '').lower()
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != wallet:
        emit('error', {'message': 'Not authenticated'})
        return
    _dm_p2p_peers[wallet] = request.sid
    join_room('dm_p2p')
    # Announce online so friends can open a channel
    emit('dm_p2p_peer_online', {'wallet': wallet}, room='dm_p2p', include_self=False)


@socketio.on('dm_p2p_signal')
def on_dm_p2p_signal(data):
    """Forward DM WebRTC signaling. No message bodies."""
    data = data or {}
    frm = (data.get('from') or '').lower()
    to = (data.get('to') or '').lower()
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != frm:
        return
    sid = _dm_p2p_peers.get(to) or _genchat_p2p_peers.get(to)
    if sid:
        emit('dm_p2p_signal', data, room=sid)
    else:
        emit('dm_p2p_signal', data, room=to)


@socketio.on('genchat_p2p_signal')
def on_genchat_p2p_signal(data):
    """Forward WebRTC signaling to target wallet. No message bodies here."""
    data = data or {}
    frm = (data.get('from') or '').lower()
    to = (data.get('to') or '').lower()
    channel = (data.get('channel') or data.get('slug') or 'general').lower().strip() or 'general'
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != frm or not to:
        return
    sid = (_channel_p2p_peers.get(channel) or {}).get(to) or _genchat_p2p_peers.get(to)
    if sid:
        emit('genchat_p2p_signal', data, room=sid)
    else:
        # fallback: wallet personal room (if they're connected elsewhere)
        emit('genchat_p2p_signal', data, room=to)


@socketio.on('join_chat')
def on_join_chat(data):
    data = data or {}
    w1 = (data.get('wallet') or '').lower()
    w2 = (data.get('contact_wallet') or '').lower()
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != w1:
        emit('error', {'message': 'Not authenticated'})
        return
    if w1 and w2:
        join_room(get_room(w1, w2))


@socketio.on('send_message')
def on_send_message(data):
    data = data or {}
    sender = (data.get('sender_wallet') or '').lower()
    recipient = (data.get('recipient_wallet') or '').lower()
    content = (data.get('content') or '').strip()
    msg_type = data.get('type', 'text')

    if not sender or not recipient or not content:
        return

    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != sender:
        emit('error', {'message': 'Not authenticated as sender'})
        return

    # Verify they are approved contacts
    conn = get_db()
    approved = conn.execute(
        'SELECT 1 FROM contacts WHERE wallet = ? AND contact_wallet = ? AND status = ?',
        (recipient, sender, 'approved')
    ).fetchone()

    if not approved:
        conn.close()
        emit('error', {'message': 'Not in contact list'})
        return

    room = get_room(sender, recipient)
    now = int(time.time())
    expires_at = NEVER_EXPIRES

    cursor = conn.execute(
        'INSERT INTO messages (room, sender_wallet, content, msg_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        (room, sender, content, msg_type, now, expires_at)
    )
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

    msg = {
        'id': msg_id,
        'sender_wallet': sender,
        'content': content,
        'type': msg_type,
        'created_at': now,
        'expires_at': expires_at
    }

    emit('new_message', msg, room=room)
    # Ping recipient's personal room for notification
    sender_handle = get_handle_for(sender)
    emit('notification', {
        'from_wallet': sender,
        'handle': sender_handle,
        'preview': content[:50]
    }, room=recipient)

    # Send push notification (background, non-blocking)
    eventlet.spawn(send_push_notification, recipient, sender_handle, content[:80])

# WebRTC signaling
@socketio.on('call_offer')
def on_call_offer(data):
    data = data or {}
    caller = (data.get('caller_wallet') or '').lower()
    callee = (data.get('callee_wallet') or '').lower()
    offer = data.get('offer')
    if not caller or not callee or not offer:
        return
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != caller:
        emit('error', {'message': 'Not authenticated'})
        return
    caller_handle = get_handle_for(caller)
    call_data = {'caller_wallet': caller, 'handle': caller_handle, 'offer': offer}
    # Buffer for 90s so if callee is backgrounded, they get the call when they reconnect
    pending_calls[callee] = call_data
    eventlet.spawn(_expire_pending_call, callee)
    # Emit to callee room (works if they're currently connected)
    emit('call_offer', call_data, room=callee)
    # Push notification to wake up callee if their tab is backgrounded
    eventlet.spawn(send_push_notification, callee, '📞 Incoming call', 'From ' + caller_handle, {
        'type': 'incoming_call',
        'caller_wallet': caller,
        'caller_handle': caller_handle,
        'url': 'https://lightchat.chat'
    })

@socketio.on('call_answer')
def on_call_answer(data):
    caller = (data.get('caller_wallet') or '').lower()
    callee = (data.get('callee_wallet') or '').lower()
    answer = data.get('answer')
    if not caller or not callee or not answer:
        return
    pending_calls.pop(callee, None)  # call answered — clear buffer
    emit('call_answer', {
        'callee_wallet': callee,
        'answer': answer
    }, room=caller)

@socketio.on('ice_candidate')
def on_ice_candidate(data):
    target = (data.get('target_wallet') or '').lower()
    candidate = data.get('candidate')
    sender = (data.get('sender_wallet') or '').lower()
    if not target or not candidate:
        return
    emit('ice_candidate', {
        'sender_wallet': sender,
        'candidate': candidate
    }, room=target)

@socketio.on('call_end')
def on_call_end(data):
    target = (data.get('target_wallet') or '').lower()
    sender = (data.get('sender_wallet') or '').lower()
    if not target:
        return
    pending_calls.pop(target, None)  # call ended — clear buffer for target
    pending_calls.pop(sender, None)  # also clear for sender
    emit('call_end', {'sender_wallet': sender}, room=target)

# ══════════════════════════════════════════════════════════════════════
# GROUP CHAT
# ══════════════════════════════════════════════════════════════════════

def _get_groups_for_wallet(w, conn, now):
    rows = conn.execute('''
        SELECT g.id, g.name, g.created_by, g.created_at,
               (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) as member_count,
               (SELECT content FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_message,
               (SELECT type FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_type,
               (SELECT timestamp FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1) as last_time
        FROM groups g
        JOIN group_members mgr ON mgr.group_id = g.id AND mgr.wallet = ?
        ORDER BY COALESCE((SELECT timestamp FROM group_messages WHERE group_id = g.id AND expires_at > ? ORDER BY timestamp DESC LIMIT 1), 0) DESC
    ''', (now, now, now, w, now)).fetchall()
    return [{
        'id': r['id'], 'name': r['name'], 'created_by': r['created_by'],
        'member_count': r['member_count'],
        'last_message': r['last_message'], 'last_type': r['last_type'],
        'last_time': r['last_time']
    } for r in rows]

@socketio.on('create_group')
def on_create_group(data):
    data = data or {}
    creator = (data.get('wallet') or '').lower()
    name = (data.get('name') or '').strip()
    members = data.get('members') or []
    if not creator or not name:
        return
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != creator:
        emit('error', {'message': 'Not authenticated'})
        return
    group_id = str(uuid.uuid4())
    now_str = str(int(time.time()))
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO groups (id, name, created_by, created_at) VALUES (?, ?, ?, ?)',
            (group_id, name, creator, now_str)
        )
        conn.execute(
            'INSERT OR IGNORE INTO group_members (group_id, wallet, joined_at) VALUES (?, ?, ?)',
            (group_id, creator, now_str)
        )
        for m in members:
            mw = str(m).lower().strip()
            if mw and mw != creator:
                conn.execute(
                    'INSERT OR IGNORE INTO group_members (group_id, wallet, joined_at) VALUES (?, ?, ?)',
                    (group_id, mw, now_str)
                )
        conn.commit()
        member_rows = conn.execute(
            'SELECT wallet FROM group_members WHERE group_id = ?', (group_id,)
        ).fetchall()
        member_count = len(member_rows)
        group_data = {
            'id': group_id, 'name': name, 'created_by': creator,
            'member_count': member_count,
            'last_message': None, 'last_type': None, 'last_time': None
        }
        for row in member_rows:
            socketio.emit('group_created', group_data, room=row['wallet'])
    finally:
        conn.close()

@socketio.on('join_group')
def on_join_group(data):
    group_id = (data.get('group_id') or '').strip()
    if group_id:
        join_room(group_id)

@socketio.on('send_group_message')
def on_send_group_message(data):
    data = data or {}
    sender = (data.get('wallet') or '').lower()
    group_id = (data.get('group_id') or '').strip()
    content = str(data.get('content') or '').strip()
    msg_type = str(data.get('type') or 'text')
    if not sender or not group_id or not content:
        return
    auth_w = _wallet_for_sid(request.sid)
    if not auth_w or auth_w != sender:
        emit('error', {'message': 'Not authenticated as sender'})
        return
    conn = get_db()
    is_member = conn.execute(
        'SELECT 1 FROM group_members WHERE group_id = ? AND wallet = ?',
        (group_id, sender)
    ).fetchone()
    if not is_member:
        conn.close()
        emit('error', {'message': 'Not a group member'})
        return
    now = int(time.time())
    expires_at = NEVER_EXPIRES
    msg_id = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO group_messages (id, group_id, sender_wallet, content, type, timestamp, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (msg_id, group_id, sender, content, msg_type, now, expires_at)
    )
    conn.commit()
    sender_handle = get_handle_for(sender, conn)
    conn.close()
    msg = {
        'id': msg_id, 'group_id': group_id,
        'sender_wallet': sender, 'sender_handle': sender_handle,
        'content': content, 'type': msg_type,
        'timestamp': now, 'created_at': now
    }
    emit('new_group_message', msg, room=group_id)

@socketio.on('get_groups')
def on_get_groups(data):
    w = (data.get('wallet') or '').lower()
    if not w:
        return
    conn = get_db()
    result = _get_groups_for_wallet(w, conn, int(time.time()))
    conn.close()
    emit('groups_list', result)

@socketio.on('get_group_messages')
def on_get_group_messages(data):
    group_id = (data.get('group_id') or '').strip()
    requester = (data.get('wallet') or '').lower()
    limit = min(int(data.get('limit') or 50), 100)
    if not group_id or not requester:
        return
    conn = get_db()
    is_member = conn.execute(
        'SELECT 1 FROM group_members WHERE group_id = ? AND wallet = ?',
        (group_id, requester)
    ).fetchone()
    if not is_member:
        conn.close()
        return
    now = int(time.time())
    rows = conn.execute('''
        SELECT gm.id, gm.group_id, gm.sender_wallet, gm.content, gm.type,
               gm.timestamp, h.handle as sender_handle
        FROM group_messages gm
        LEFT JOIN handles h ON h.wallet = gm.sender_wallet
        WHERE gm.group_id = ? AND gm.expires_at > ?
        ORDER BY gm.timestamp ASC
        LIMIT ?
    ''', (group_id, now, limit)).fetchall()
    conn.close()
    messages = [{
        'id': r['id'], 'group_id': r['group_id'],
        'sender_wallet': r['sender_wallet'],
        'sender_handle': r['sender_handle'] or (r['sender_wallet'][:8] + '...'),
        'content': r['content'], 'type': r['type'],
        'timestamp': r['timestamp'], 'created_at': r['timestamp']
    } for r in rows]
    emit('group_messages', {'group_id': group_id, 'messages': messages})

# ══════════════════════════════════════════════════════════════════════
# GROUP VIDEO CALLS (WebRTC mesh, max 5)
# ══════════════════════════════════════════════════════════════════════

@socketio.on('group_call_offer')
def on_group_call_offer(data):
    caller = (data.get('wallet') or '').lower()
    group_id = (data.get('group_id') or '').strip()
    group_name = (data.get('group_name') or 'Group').strip()
    if not caller or not group_id:
        return
    conn = get_db()
    is_member = conn.execute(
        'SELECT 1 FROM group_members WHERE group_id=? AND wallet=?', (group_id, caller)
    ).fetchone()
    member_rows = conn.execute(
        'SELECT wallet FROM group_members WHERE group_id=?', (group_id,)
    ).fetchall()
    conn.close()
    if not is_member:
        return
    member_wallets = [r['wallet'] for r in member_rows]
    if len(member_wallets) > 5:
        emit('error', {'message': 'Group too large for video call (max 5 members)'})
        return
    # Init call state (caller is the only active member to start)
    group_calls[group_id] = {
        'members': {caller},
        'caller': caller,
        'group_name': group_name
    }
    caller_handle = get_handle_for(caller)
    # Notify every other group member
    for mw in member_wallets:
        if mw != caller:
            socketio.emit('group_call_offer', {
                'caller_wallet': caller,
                'caller_handle': caller_handle,
                'group_id': group_id,
                'group_name': group_name
            }, room=mw)
    # Auto-cancel after 30 s if nobody joins
    eventlet.spawn(_expire_group_call, group_id, caller)


@socketio.on('group_call_accept')
def on_group_call_accept(data):
    new_member = (data.get('wallet') or '').lower()
    group_id = (data.get('group_id') or '').strip()
    if not new_member or not group_id:
        return
    call = group_calls.get(group_id)
    if not call:
        emit('group_call_ended', {'group_id': group_id, 'reason': 'no_call'})
        return
    new_handle = get_handle_for(new_member)
    existing = list(call['members'])
    call['members'].add(new_member)
    # Build list of existing members with handles for the new joiner
    member_list = [{'wallet': mw, 'handle': get_handle_for(mw)} for mw in existing]
    # Tell new joiner about everyone already in the call
    emit('group_call_joined', {'group_id': group_id, 'members': member_list})
    # Tell everyone already in the call that a new peer joined
    for mw in existing:
        socketio.emit('group_call_peer_joined', {
            'wallet': new_member,
            'handle': new_handle,
            'group_id': group_id
        }, room=mw)


@socketio.on('group_call_decline')
def on_group_call_decline(data):
    # Receiving side declined — nothing required server-side
    pass


@socketio.on('group_call_leave')
def on_group_call_leave(data):
    leaver = (data.get('wallet') or '').lower()
    group_id = (data.get('group_id') or '').strip()
    if not leaver or not group_id:
        return
    call = group_calls.get(group_id)
    if not call:
        return
    call['members'].discard(leaver)
    remaining = list(call['members'])
    if len(remaining) <= 1:
        # End call for everyone left
        for mw in remaining:
            socketio.emit('group_call_ended', {'group_id': group_id}, room=mw)
        group_calls.pop(group_id, None)
    else:
        # Notify remaining peers that someone left
        for mw in remaining:
            socketio.emit('group_call_peer_left', {
                'wallet': leaver,
                'group_id': group_id
            }, room=mw)


@socketio.on('group_peer_signal')
def on_group_peer_signal(data):
    from_wallet = (data.get('from') or '').lower()
    to_wallet = (data.get('to') or '').lower()
    signal = data.get('signal')
    group_id = (data.get('group_id') or '').strip()
    if not from_wallet or not to_wallet or not signal:
        return
    socketio.emit('group_peer_signal', {
        'from': from_wallet,
        'signal': signal,
        'group_id': group_id
    }, room=to_wallet)


# ══════════════════════════════════════════════════════════════════════
# LCAI price helper (calls/media free — no premium / fee wallet)
# ══════════════════════════════════════════════════════════════════════

_lcai_price_cache = {'price': 0.004, 'ts': 0}


def get_lcai_price():
    """Fetch LCAI/USD price, cached 5 min. Fallback $0.004."""
    global _lcai_price_cache
    now = time.time()
    if now - _lcai_price_cache['ts'] < 300:
        return _lcai_price_cache['price']
    # Try CoinGecko
    try:
        req = _urllib_req.Request(
            'https://api.coingecko.com/api/v3/simple/price?ids=lightchain-ai&vs_currencies=usd',
            headers={'User-Agent': 'LightChat/1.0', 'Accept': 'application/json'}
        )
        with _urllib_req.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            price = (data.get('lightchain-ai') or {}).get('usd')
            if price and float(price) > 0:
                _lcai_price_cache = {'price': float(price), 'ts': now}
                return float(price)
    except Exception:
        pass
    # Try DexScreener
    try:
        req = _urllib_req.Request(
            'https://api.dexscreener.com/latest/dex/search?q=LCAI',
            headers={'User-Agent': 'LightChat/1.0', 'Accept': 'application/json'}
        )
        with _urllib_req.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            for pair in (data.get('pairs') or []):
                price = float(pair.get('priceUsd') or 0)
                if price > 0:
                    _lcai_price_cache = {'price': price, 'ts': now}
                    return price
    except Exception:
        pass
    # Fallback — bump ts to avoid hammering APIs on every request
    _lcai_price_cache['ts'] = now
    return _lcai_price_cache['price']


def lightchain_rpc(method, params):
    """Call the Lightchain JSON-RPC endpoint."""
    payload = json.dumps({
        'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1
    }).encode()
    req = _urllib_req.Request(
        'https://node1.lightchain.ai',
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'LightChat/1.0'}
    )
    with _urllib_req.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


@app.route('/api/lcai-price')
def api_lcai_price():
    price = get_lcai_price()
    return jsonify({'price': price, 'currency': 'USD'})


@app.route('/api/call-access/<wallet>')
def api_call_access(wallet):
    """Always allowed — LightChat is free (no call paywall)."""
    return jsonify({
        'allowed': True,
        'subscribed': True,
        'free_remaining': 999,
        'expires_at': None,
        'free': True,
    })



@app.route('/api/use-call', methods=['POST'])
def api_use_call():
    """No-op — calls are free."""
    return jsonify({'ok': True, 'free': True})



@app.route('/api/verify-subscription', methods=['POST'])
def api_verify_subscription():
    """Disabled — LightChat is free (no Pro / subscriptions)."""
    return jsonify({
        'ok': False,
        'disabled': True,
        'error': 'Subscriptions disabled. LightChat is free for everyone.',
    }), 410



@app.route('/api/confirm-gift', methods=['POST'])
def api_confirm_gift():
    """Disabled — no LCAI gift transfers in community LightChat."""
    return jsonify({
        'ok': False,
        'disabled': True,
        'error': 'Gifts / LCAI transfers disabled. LightChat is free.',
    }), 410



# Free STUN + public TURN if METERED_API_KEY missing or Metered call fails
_TURN_FALLBACK = [
    {'urls': 'stun:stun.l.google.com:19302'},
    {'urls': 'stun:stun1.l.google.com:19302'},
    {'urls': 'stun:stun.cloudflare.com:3478'},
    {
        'urls': 'turn:openrelay.metered.ca:80',
        'username': 'openrelayproject',
        'credential': 'openrelayproject',
    },
    {
        'urls': 'turn:openrelay.metered.ca:443',
        'username': 'openrelayproject',
        'credential': 'openrelayproject',
    },
    {
        'urls': 'turn:openrelay.metered.ca:443?transport=tcp',
        'username': 'openrelayproject',
        'credential': 'openrelayproject',
    },
    {
        'urls': 'turn:openrelay.metered.ca:80?transport=tcp',
        'username': 'openrelayproject',
        'credential': 'openrelayproject',
    },
]


@app.route('/api/turn-credentials')
def api_turn_credentials():
    """Return ICE servers. Uses Metered when METERED_SUBDOMAIN + METERED_API_KEY set; else free STUN/TURN."""
    api_key = os.environ.get('METERED_API_KEY', '').strip()
    metered_sub = (os.environ.get('METERED_SUBDOMAIN') or '').strip()
    if api_key and metered_sub and _REQUESTS_AVAILABLE:
        try:
            resp = _requests.get(
                f'https://{metered_sub}.metered.live/api/v1/turn/credentials?apiKey={api_key}',
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            # Metered returns a list of iceServer objects
            if isinstance(data, list) and data:
                # Prepend extra free STUN for resilience
                merged = [
                    {'urls': 'stun:stun.l.google.com:19302'},
                    {'urls': 'stun:stun1.l.google.com:19302'},
                ] + data
                return jsonify(merged)
        except Exception as e:
            print(f'  [TURN] Metered credentials failed, using free fallback: {e}')
    return jsonify(_TURN_FALLBACK)


# ══════════════════════════════════════════════════════════════════════
# WATCH TOGETHER — synced YouTube viewing
# ══════════════════════════════════════════════════════════════════════
# {party_id: {video_url, host, members: set(), chat_id, is_group}}
watch_parties = {}

@socketio.on('start_watch_party')
def on_start_watch_party(data):
    sender = (data.get('wallet') or '').lower()
    chat_id = (data.get('chat_id') or '').strip()
    video_url = (data.get('video_url') or '').strip()
    is_group = bool(data.get('is_group', False))
    recipients = data.get('recipients') or []  # list of wallet addrs (for DMs)
    if not sender or not chat_id or not video_url:
        return

    party_id = str(uuid.uuid4())
    watch_parties[party_id] = {
        'video_url': video_url,
        'host': sender,
        'members': {sender},
        'chat_id': chat_id,
        'is_group': is_group
    }

    host_handle = get_handle_for(sender)
    invite_payload = {
        'party_id': party_id,
        'video_url': video_url,
        'host_wallet': sender,
        'host_handle': host_handle
    }

    if is_group:
        # Emit to the Socket.IO group room (all members already joined it)
        socketio.emit('watch_party_invite', invite_payload, room=chat_id)
    else:
        # DM: emit to each recipient's personal wallet room
        for rw in recipients:
            rw = str(rw).lower().strip()
            if rw and rw != sender:
                socketio.emit('watch_party_invite', invite_payload, room=rw)

    emit('watch_party_created', {'party_id': party_id})


@socketio.on('join_watch_party')
def on_join_watch_party(data):
    joiner = (data.get('wallet') or '').lower()
    party_id = (data.get('party_id') or '').strip()
    if not joiner or not party_id:
        return
    party = watch_parties.get(party_id)
    if not party:
        emit('watch_party_error', {'message': 'Watch party not found or ended'})
        return
    party['members'].add(joiner)
    join_room(party_id)
    count = len(party['members'])
    socketio.emit('watch_party_update', {
        'party_id': party_id,
        'member_count': count
    }, room=party_id)
    emit('watch_party_joined', {
        'party_id': party_id,
        'video_url': party['video_url'],
        'host_wallet': party['host'],
        'member_count': count
    })


@socketio.on('leave_watch_party')
def on_leave_watch_party(data):
    leaver = (data.get('wallet') or '').lower()
    party_id = (data.get('party_id') or '').strip()
    if not leaver or not party_id:
        return
    party = watch_parties.get(party_id)
    if not party:
        return
    party['members'].discard(leaver)
    is_host = party['host'] == leaver
    if is_host or not party['members']:
        reason = 'host_left' if is_host else 'empty'
        socketio.emit('watch_party_ended', {'party_id': party_id, 'reason': reason}, room=party_id)
        watch_parties.pop(party_id, None)
    else:
        socketio.emit('watch_party_update', {
            'party_id': party_id,
            'member_count': len(party['members'])
        }, room=party_id)


@socketio.on('watch_sync')
def on_watch_sync(data):
    sender = (data.get('wallet') or '').lower()
    party_id = (data.get('party_id') or '').strip()
    action = data.get('action')  # 'play' | 'pause' | 'seek'
    time_pos = float(data.get('time', 0))
    if not sender or not party_id or not action:
        return
    party = watch_parties.get(party_id)
    if not party or party['host'] != sender:
        return  # Only host can broadcast sync
    socketio.emit('watch_sync', {
        'action': action,
        'time': time_pos
    }, room=party_id)


# ══════════════════════════════════════════════════════════════════════
# CALENDAR EVENTS
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/calendar')
def api_get_calendar():
    wallet_q = request.args.get('wallet', '').lower().strip()
    month_q  = request.args.get('month', '')  # e.g. "2026-06"
    if not wallet_q:
        return jsonify({'error': 'wallet required'}), 400
    conn = get_db()
    try:
        if month_q:
            try:
                import calendar as _cal
                from datetime import datetime as _dt
                yr, mo = int(month_q[:4]), int(month_q[5:7])
                first_day = int(_dt(yr, mo, 1).timestamp())
                last_day_num = _cal.monthrange(yr, mo)[1]
                last_day = int(_dt(yr, mo, last_day_num, 23, 59, 59).timestamp())
                rows = conn.execute(
                    '''SELECT * FROM calendar_events WHERE wallet = ?
                       AND start_time >= ? AND start_time <= ?
                       ORDER BY start_time ASC''',
                    (wallet_q, first_day, last_day)
                ).fetchall()
            except Exception:
                rows = conn.execute(
                    'SELECT * FROM calendar_events WHERE wallet = ? ORDER BY start_time ASC',
                    (wallet_q,)
                ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM calendar_events WHERE wallet = ? ORDER BY start_time ASC',
                (wallet_q,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/calendar', methods=['POST'])
def api_create_calendar():
    data = request.json or {}
    wallet_b   = data.get('wallet', '').lower().strip()
    title      = data.get('title', '').strip()
    start_time = data.get('start_time')
    if not wallet_b or not title or not start_time:
        return jsonify({'error': 'wallet, title, start_time required'}), 400
    ev_id = str(uuid.uuid4())
    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO calendar_events
               (id, wallet, title, start_time, end_time, notes, color, shared_with, reminder_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (ev_id, wallet_b, title,
             int(start_time),
             int(data['end_time']) if data.get('end_time') else None,
             data.get('notes', ''),
             data.get('color', '#9b7fe8'),
             json.dumps(data.get('shared_with', [])),
             int(data.get('reminder_minutes', 30)))
        )
        conn.commit()
        return jsonify({'id': ev_id, 'created': True})
    finally:
        conn.close()


@app.route('/api/calendar/<ev_id>', methods=['PUT'])
def api_update_calendar(ev_id):
    data     = request.json or {}
    wallet_b = data.get('wallet', '').lower().strip()
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT wallet FROM calendar_events WHERE id = ?', (ev_id,)
        ).fetchone()
        if not row or row['wallet'] != wallet_b:
            return jsonify({'error': 'not found or unauthorized'}), 404
        fields, vals = [], []
        for field in ['title', 'notes', 'color']:
            if field in data:
                fields.append(field + ' = ?'); vals.append(data[field])
        for field in ['start_time', 'end_time', 'reminder_minutes']:
            if field in data:
                fields.append(field + ' = ?')
                vals.append(int(data[field]) if data[field] is not None else None)
        if 'shared_with' in data:
            fields.append('shared_with = ?'); vals.append(json.dumps(data['shared_with']))
        if not fields:
            return jsonify({'updated': False})
        vals.append(ev_id)
        conn.execute('UPDATE calendar_events SET ' + ', '.join(fields) + ' WHERE id = ?', vals)
        conn.commit()
        return jsonify({'updated': True})
    finally:
        conn.close()


@app.route('/api/calendar/<ev_id>', methods=['DELETE'])
def api_delete_calendar(ev_id):
    wallet_b = request.args.get('wallet', '').lower().strip()
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT wallet FROM calendar_events WHERE id = ?', (ev_id,)
        ).fetchone()
        if not row or row['wallet'] != wallet_b:
            return jsonify({'error': 'not found or unauthorized'}), 404
        conn.execute('DELETE FROM calendar_events WHERE id = ?', (ev_id,))
        conn.commit()
        return jsonify({'deleted': True})
    finally:
        conn.close()


@app.route('/api/calendar/<ev_id>/share', methods=['POST'])
def api_share_calendar(ev_id):
    data         = request.json or {}
    wallet_b     = data.get('wallet', '').lower().strip()
    share_wallet = data.get('share_with', '').lower().strip()
    if not wallet_b or not share_wallet:
        return jsonify({'error': 'wallet and share_with required'}), 400
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT * FROM calendar_events WHERE id = ?', (ev_id,)
        ).fetchone()
        if not row or row['wallet'] != wallet_b:
            return jsonify({'error': 'not found or unauthorized'}), 404
        ev = dict(row)
        socketio.emit('calendar_invite', {
            'event_id': ev_id,
            'title': ev['title'],
            'start_time': ev['start_time'],
            'end_time': ev['end_time'],
            'notes': ev['notes'],
            'color': ev['color'],
            'from_wallet': wallet_b,
            'from_handle': get_handle_for(wallet_b)
        }, room=share_wallet)
        return jsonify({'shared': True})
    finally:
        conn.close()


@app.route('/clear-history', methods=['POST'])
def clear_history():
    data = request.json or {}
    wallet_req = data.get('wallet', '').lower().strip()
    contact_wallet = data.get('contact_wallet', '').lower().strip()
    clear_all = bool(data.get('clear_all', False))

    if not wallet_req:
        return jsonify({'error': 'wallet required'}), 400

    conn = get_db()
    try:
        if clear_all:
            # Clear messages with ALL contacts; contacts themselves are kept
            approved = conn.execute(
                'SELECT contact_wallet FROM contacts WHERE wallet = ? AND status = ?',
                (wallet_req, 'approved')
            ).fetchall()
            for row in approved:
                room = get_room(wallet_req, row['contact_wallet'])
                conn.execute('DELETE FROM messages WHERE room = ?', (room,))
        elif contact_wallet:
            room = get_room(wallet_req, contact_wallet)
            conn.execute('DELETE FROM messages WHERE room = ?', (room,))
        else:
            return jsonify({'error': 'contact_wallet or clear_all required'}), 400
        conn.commit()
        return jsonify({'cleared': True})
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# VOICE AI — AIVM Client + session memory
# ════════════════════════════════════════════════════════════════════════════

# Flow: JWT auth → select/prepare session → on-chain createSession/submitJob → relay WS → decrypt
_AIVM_ABI = [
    {
        "name": "createSession", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "paramsHash",      "type": "bytes32"},
            {"name": "worker",          "type": "address"},
            {"name": "encWorkerKey",    "type": "bytes"},
            {"name": "ephemeralPubKey", "type": "bytes"},
            {"name": "initState",       "type": "bytes"},
            {"name": "expiry",          "type": "uint256"},
        ],
        "outputs": [{"name": "sessionId", "type": "uint256"}],
    },
    {
        "name": "submitJob", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "sessionId",  "type": "uint256"},
            {"name": "promptHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "jobId", "type": "uint256"}],
    },
    {
        "anonymous": False, "name": "SessionCreated", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "sessionId",      "type": "uint256"},
            {"indexed": True,  "name": "user",            "type": "address"},
            {"indexed": True,  "name": "paramsHash",      "type": "bytes32"},
            {"indexed": False, "name": "worker",          "type": "address"},
            {"indexed": False, "name": "encWorkerKey",    "type": "bytes"},
            {"indexed": False, "name": "ephemeralPubKey", "type": "bytes"},
        ],
    },
    {
        "anonymous": False, "name": "JobSubmitted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",     "type": "uint256"},
            {"indexed": True,  "name": "sessionId", "type": "uint256"},
            {"indexed": False, "name": "worker",    "type": "address"},
        ],
    },
    {
        "anonymous": False, "name": "JobCompleted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",         "type": "uint256"},
            {"indexed": True,  "name": "worker",         "type": "address"},
            {"indexed": False, "name": "responseHash",   "type": "bytes32"},
            {"indexed": False, "name": "ciphertextHash", "type": "bytes32"},
        ],
    },
]


def _aivm_decode_pubkey(s):
    """Accept hex (with/without 0x) or base64; return 65-byte uncompressed P-256 point."""
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = s.strip()
    if s.startswith('0x') or s.startswith('0X'):
        b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in '0123456789abcdefABCDEF' for c in s):
        b = bytes.fromhex(s)
    else:
        b = base64.b64decode(s)
    if len(b) != 65:
        raise ValueError(f"pubkey decode: expected 65 bytes, got {len(b)}")
    return b


def _aivm_ecdh_wrap(session_key: bytes, peer_pub_bytes: bytes) -> bytes:
    """ECDH-wrap session_key for peer P-256 pubkey. Returns ephemPub(65)||nonce(12)||ct||tag(16)."""
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend

    x = int.from_bytes(peer_pub_bytes[1:33], 'big')
    y = int.from_bytes(peer_pub_bytes[33:65], 'big')
    peer_pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())

    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared = ephem_priv.exchange(ECDH(), peer_pub)

    pub_nums = ephem_priv.public_key().public_numbers()
    ephem_pub_bytes = (b'\x04' +
                       pub_nums.x.to_bytes(32, 'big') +
                       pub_nums.y.to_bytes(32, 'big'))

    nonce  = secrets.token_bytes(12)
    ct_tag = AESGCM(shared).encrypt(nonce, session_key, None)
    return ephem_pub_bytes + nonce + ct_tag


def _aivm_aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aivm_aes_decrypt(key: bytes, blob: bytes) -> bytes:
    """AES-256-GCM decrypt nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


class AIVMClient:
    """
    Runs LLM inference through the Lightchain decentralized worker network.
    Requires a funded Lightchain mainnet wallet (LIGHTCHAIN_PRIVATE_KEY env var).
    Cost: ~0.022 LCAI per inference (0.02 worker fee + ~0.002 gas).
    """

    def __init__(self, private_key: str):
        import requests as _req
        from web3 import Web3
        from eth_account import Account

        self._req      = _req
        self._w3       = Web3(Web3.HTTPProvider(_AIVM_RPC))
        self._account  = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(_AIVM_JOB_REG),
            abi=_AIVM_ABI,
        )
        self._jwt     = None
        self._jwt_exp = 0
        print(f"  [AIVM] wallet: {self._account.address}")

    def _get_jwt(self) -> str:
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30:
            return self._jwt
        req = self._req
        r = req.get(
            f"{_AIVM_GATEWAY}/api/auth/challenge",
            params={"address": self._account.address}, timeout=15,
        )
        r.raise_for_status()
        message = r.json()["message"]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = req.post(
            f"{_AIVM_GATEWAY}/api/auth/verify",
            json={"message": message, "signature": "0x" + sig.signature.hex()},
            timeout=15,
        )
        r2.raise_for_status()
        v = r2.json()
        self._jwt = v["token"]
        exp_str = v["expiresAt"][:19].replace("T", " ")
        self._jwt_exp = time.mktime(time.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        return self._jwt

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

    def run_inference(self, prompt: str, timeout_secs: int = 360) -> str:
        import websocket as _ws
        from web3 import Web3

        req = self._req
        print(f"  [AIVM] starting inference ({len(prompt)} chars)")

        # 1-2. Auth + pick model
        r = req.get(f"{_AIVM_GATEWAY}/api/models", timeout=15)
        r.raise_for_status()
        models = r.json().get("models", [])
        model  = next((m for m in models if m["name"] == "llama3-8b"), models[0] if models else None)
        if not model:
            raise RuntimeError("No models available from AIVM gateway")
        model_id = model["id"]
        print(f"  [AIVM] model: {model['name']} id={model_id[:10]}…")

        # 3. Select worker
        r = req.post(
            f"{_AIVM_GATEWAY}/api/sessions/select",
            json={"modelId": model_id},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        sel = r.json()
        print(f"  [AIVM] worker: {sel['worker']}")

        # 4-5. Session key + ECDH wrap
        session_key  = secrets.token_bytes(32)
        enc_worker   = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["workerEncryptionKey"]))
        enc_disputer = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["disputerEncryptionKey"]))

        # 6. Prepare
        r = req.post(
            f"{_AIVM_GATEWAY}/api/sessions/prepare",
            json={
                "modelId":        model_id,
                "encWorkerKey":   base64.b64encode(enc_worker).decode(),
                "encDisputerKey": base64.b64encode(enc_disputer).decode(),
            },
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        prep = r.json()

        # 7. createSession on-chain
        params_hash = bytes.fromhex(model_id[2:].zfill(64) if model_id[:2].lower() == "0x" else model_id.zfill(64))
        sig_bytes   = bytes.fromhex(prep["signature"][2:] if prep["signature"][:2].lower() == "0x" else prep["signature"])

        gas_price = self._w3.eth.gas_price
        nonce_val = self._w3.eth.get_transaction_count(self._account.address)

        tx = self._registry.functions.createSession(
            params_hash,
            Web3.to_checksum_address(prep["worker"]),
            enc_worker,
            enc_disputer,
            sig_bytes,
            prep["expiry"],
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val,
            "gas":      1_000_000,
            "gasPrice": gas_price,
            "value":    0,
            "chainId":  _AIVM_CHAIN_ID,
        })
        signed  = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  [AIVM] createSession tx: {tx_hash.hex()}")
        receipt1 = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt1.status != 1:
            raise RuntimeError("createSession reverted on-chain")

        session_id = None
        for log in receipt1.logs:
            try:
                evt = self._registry.events.SessionCreated().process_log(log)
                session_id = evt["args"]["sessionId"]
                break
            except Exception:
                pass
        if session_id is None:
            raise RuntimeError("SessionCreated event not found in receipt")
        print(f"  [AIVM] sessionId: {session_id}")

        # 8. Open relay WebSocket
        relay_token = None
        deadline = time.time() + 30
        while time.time() < deadline:
            r = req.get(
                f"{_AIVM_GATEWAY}/api/sessions/{session_id}/token",
                headers=self._auth_headers(), timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("token"):
                    relay_token = d["token"]
                    break
            time.sleep(1)
        if not relay_token:
            raise RuntimeError("Relay token not ready within 30s")

        chunks   = []
        ws_ready = threading.Event()
        ws_err   = [None]

        def _on_message(ws_obj, message):
            try:
                frame = json.loads(message)
                payload = frame.get("payload")
                if not payload:
                    return
                blob = base64.b64decode(payload)
                try:
                    pt = _aivm_aes_decrypt(session_key, blob)
                    chunks.append(pt.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            except Exception:
                pass

        def _on_open(ws_obj):
            ws_ready.set()

        def _on_error(ws_obj, err):
            ws_err[0] = err
            ws_ready.set()

        ws = _ws.WebSocketApp(
            f"{_AIVM_RELAY}?token={_url_quote(relay_token)}",
            on_message=_on_message,
            on_open=_on_open,
            on_error=_on_error,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0]:
            raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        print("  [AIVM] relay connected")

        # 9. Encrypt prompt + upload blob
        cipher = _aivm_aes_encrypt(session_key, prompt.encode("utf-8"))
        r = req.post(
            f"{_AIVM_GATEWAY}/api/blobs",
            json={"data": base64.b64encode(cipher).decode()},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        blob_hashes = r.json().get("blobHashes", [])
        if not blob_hashes:
            raise RuntimeError("No blob hash returned from gateway")
        _bh = blob_hashes[0]
        prompt_hash = bytes.fromhex(_bh[2:].zfill(64) if _bh[:2].lower() == "0x" else _bh.zfill(64))

        # 10. submitJob (pay 0.02 LCAI)
        nonce_val2 = self._w3.eth.get_transaction_count(self._account.address)
        tx2 = self._registry.functions.submitJob(
            session_id,
            prompt_hash,
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val2,
            "gas":      500_000,
            "gasPrice": gas_price,
            "value":    _AIVM_JOB_FEE,
            "chainId":  _AIVM_CHAIN_ID,
        })
        signed2  = self._account.sign_transaction(tx2)
        tx_hash2 = self._w3.eth.send_raw_transaction(signed2.raw_transaction)
        print(f"  [AIVM] submitJob tx: {tx_hash2.hex()}")
        receipt2 = self._w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=90)
        if receipt2.status != 1:
            raise RuntimeError("submitJob reverted — check LCAI balance")

        job_id = None
        for log in receipt2.logs:
            try:
                evt = self._registry.events.JobSubmitted().process_log(log)
                job_id = evt["args"]["jobId"]
                break
            except Exception:
                pass
        if job_id is None:
            raise RuntimeError("JobSubmitted event not found in receipt")
        print(f"  [AIVM] jobId: {job_id}")

        # 11. Poll for JobCompleted
        job_completed_topic = "0x" + Web3.keccak(
            text="JobCompleted(uint256,address,bytes32,bytes32)"
        ).hex()
        job_id_topic = "0x" + hex(job_id)[2:].zfill(64)

        done     = False
        deadline = time.time() + timeout_secs
        while time.time() < deadline and not done:
            time.sleep(5)
            try:
                head = self._w3.eth.block_number
                logs = self._w3.eth.get_logs({
                    "address":   Web3.to_checksum_address(_AIVM_JOB_REG),
                    "fromBlock": receipt2.blockNumber,
                    "toBlock":   head,
                    "topics":    [job_completed_topic, job_id_topic],
                })
                if logs:
                    done = True
                    print(f"  [AIVM] JobCompleted! worker: {logs[0].get('address')}")
            except Exception as e:
                print(f"  [AIVM] log poll error (retrying): {e}")

        time.sleep(4)
        ws.close()

        result = "".join(chunks)
        if result:
            print(f"  [AIVM] inference done (relay data), {len(result)} chars")
            return result

        if not done:
            raise RuntimeError(f"Timeout after {timeout_secs}s waiting for JobCompleted")

        print(f"  [AIVM] inference done, {len(result)} chars received")
        return result


# ── Session memory for Voice AI ──────────────────────────────────────────────
_sessions: dict = {}          # session_id → list of {role, content} dicts
_sessions_lock = threading.Lock()
_SESSION_MAX_EXCHANGES = 50   # keep last 50 exchanges per session
_CONTEXT_WINDOW = 12          # inject last 12 exchanges into each prompt


def _get_conversation_context(session_id: str) -> str:
    """Return a formatted string of recent exchanges for injection into prompt."""
    with _sessions_lock:
        history = _sessions.get(session_id, [])
        recent  = history[-(_CONTEXT_WINDOW * 2):]   # pairs: user + assistant
    if not recent:
        return ""
    lines = []
    for msg in recent:
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)


def _save_to_session(session_id: str, user_msg: str, assistant_msg: str):
    """Append a user/assistant exchange to the session history."""
    with _sessions_lock:
        hist = _sessions.setdefault(session_id, [])
        hist.append({"role": "user",      "content": user_msg})
        hist.append({"role": "assistant", "content": assistant_msg})
        # Trim to max
        if len(hist) > _SESSION_MAX_EXCHANGES * 2:
            _sessions[session_id] = hist[-(  _SESSION_MAX_EXCHANGES * 2):]


# ── Lazy AIVM singleton ──────────────────────────────────────────────────────
_aivm_client_instance = None
_aivm_client_lock     = threading.Lock()


def _get_aivm_client():
    """Return AIVMClient singleton if LIGHTCHAIN_PRIVATE_KEY is set, else None."""
    global _aivm_client_instance
    pk = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()
    if not pk:
        return None
    with _aivm_client_lock:
        if _aivm_client_instance is None:
            try:
                _aivm_client_instance = AIVMClient(pk)
            except Exception as e:
                print(f"  [AIVM] Failed to init client: {e}")
                return None
    return _aivm_client_instance


# ── /api/voice-chat endpoint ─────────────────────────────────────────────────
@app.route('/api/voice-chat', methods=['POST', 'OPTIONS'])
def api_voice_chat():
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    try:
        data       = request.get_json(force=True) or {}
        transcript = (data.get('transcript') or '').strip()
        session_id = (data.get('session_id') or str(uuid.uuid4())).strip()
    except Exception:
        return jsonify({'error': 'invalid JSON body'}), 400

    if not transcript:
        return jsonify({'error': 'transcript is required'}), 400

    client = _get_aivm_client()
    if not client:
        resp = jsonify({'error': 'Voice AI not configured — LIGHTCHAIN_PRIVATE_KEY missing'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 503

    # Build prompt with conversation context
    context = _get_conversation_context(session_id)
    if context:
        prompt = (
            "You are a helpful voice assistant in the LightChat messaging app. "
            "Keep your answers concise and conversational — aim for 1-3 sentences "
            "since the user is listening, not reading.\n\n"
            "Conversation so far:\n"
            f"{context}\n\n"
            f"User: {transcript}\nAssistant:"
        )
    else:
        prompt = (
            "You are a helpful voice assistant in the LightChat messaging app. "
            "Keep your answers concise and conversational — aim for 1-3 sentences "
            "since the user is listening, not reading.\n\n"
            f"User: {transcript}\nAssistant:"
        )

    try:
        response_text = client.run_inference(prompt)
        # Strip leading "Assistant:" if the model echoes it
        if response_text.lstrip().startswith("Assistant:"):
            response_text = response_text.lstrip()[len("Assistant:"):].lstrip()
        response_text = response_text.strip()
    except Exception as e:
        print(f"  [voice-chat] AIVM error: {e}")
        resp = jsonify({'error': f'AI inference failed: {str(e)}'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 500

    _save_to_session(session_id, transcript, response_text)

    resp = jsonify({'response': response_text, 'session_id': session_id})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# ── Lightchain Governor proposals (read-only, cache + poll) ──────────────────
_DAO_GOVERNOR_DEFAULT = "0xD216A0c0050EdC3a9E0449EcFDf178A1652b4b68"
_DAO_POLL_THREAD = None
_DAO_POLL_LOCK = threading.Lock()

_DAO_GOVERNOR_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "name": "proposalId", "type": "uint256"},
            {"indexed": False, "name": "proposer", "type": "address"},
            {"indexed": False, "name": "targets", "type": "address[]"},
            {"indexed": False, "name": "values", "type": "uint256[]"},
            {"indexed": False, "name": "signatures", "type": "string[]"},
            {"indexed": False, "name": "calldatas", "type": "bytes[]"},
            {"indexed": False, "name": "voteStart", "type": "uint256"},
            {"indexed": False, "name": "voteEnd", "type": "uint256"},
            {"indexed": False, "name": "description", "type": "string"},
        ],
        "name": "ProposalCreated",
        "type": "event",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "state",
        "outputs": [{"type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "proposalVotes",
        "outputs": [
            {"name": "againstVotes", "type": "uint256"},
            {"name": "forVotes", "type": "uint256"},
            {"name": "abstainVotes", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "proposalSnapshot",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "proposalId", "type": "uint256"}],
        "name": "proposalDeadline",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _dao_governor_address() -> str:
    from web3 import Web3
    raw = (os.environ.get("DAO_GOVERNOR_ADDRESS") or _DAO_GOVERNOR_DEFAULT).strip()
    try:
        return Web3.to_checksum_address(raw)
    except Exception:
        return Web3.to_checksum_address(_DAO_GOVERNOR_DEFAULT)


def _dao_web3():
    """Reuse the same Lightchain RPC as AIVM (public reads — no key)."""
    from web3 import Web3
    return Web3(Web3.HTTPProvider(_AIVM_RPC, request_kwargs={"timeout": 45}))


def _dao_contract(w3=None):
    from web3 import Web3
    w3 = w3 or _dao_web3()
    return w3, w3.eth.contract(address=_dao_governor_address(), abi=_DAO_GOVERNOR_ABI)


def dao_refresh_proposal_live(conn, proposal_id: str) -> dict | None:
    """Force a live state/votes refresh for one proposal; updates cache."""
    from community import DAO_TERMINAL_STATES, dao_upsert_proposal, dao_list_proposals
    try:
        w3, gov = _dao_contract()
        pid = int(str(proposal_id))
        state = int(gov.functions.state(pid).call())
        against_w, for_w, abstain_w = gov.functions.proposalVotes(pid).call()
        try:
            snap = int(gov.functions.proposalSnapshot(pid).call())
        except Exception:
            snap = 0
        try:
            deadline = int(gov.functions.proposalDeadline(pid).call())
        except Exception:
            deadline = 0
        # Keep existing title/description from cache if present
        existing = None
        for r in dao_list_proposals(conn, active_only=False, limit=500):
            if str(r.get("proposal_id")) == str(proposal_id):
                existing = r
                break
        row = {
            "proposal_id": str(pid),
            "short_id": str(pid)[:6],
            "proposer": (existing or {}).get("proposer") or "",
            "title": (existing or {}).get("title") or "",
            "description": (existing or {}).get("description") or "",
            "state": state,
            "for_wei": str(for_w),
            "against_wei": str(against_w),
            "abstain_wei": str(abstain_w),
            "snapshot_block": snap,
            "deadline_block": deadline,
            "created_at": (existing or {}).get("created_at") or int(time.time()),
        }
        dao_upsert_proposal(conn, row)
        return row
    except Exception as e:
        print(f"  [dao] live refresh failed: {e}", flush=True)
        return None


def dao_scan_and_refresh(get_db_fn=None) -> dict:
    """Incremental ProposalCreated log scan + refresh non-terminal proposal states."""
    from community import (
        DAO_TERMINAL_STATES,
        dao_get_scan_value,
        dao_set_scan_value,
        dao_upsert_proposal,
        dao_list_proposals,
        init_community_db,
    )
    get_db_fn = get_db_fn or get_db
    init_community_db(get_db_fn)
    conn = get_db_fn()
    stats = {"new_logs": 0, "refreshed": 0, "error": None}
    try:
        w3, gov = _dao_contract()
        latest = int(w3.eth.block_number)
        start_env = (os.environ.get("DAO_SCAN_FROM_BLOCK") or "").strip()
        last = dao_get_scan_value(conn, "last_scanned_block", "")
        if last.isdigit():
            from_block = int(last) + 1
        elif start_env.isdigit():
            from_block = int(start_env)
        else:
            # First run: look back ~250k blocks (~enough for current proposals)
            from_block = max(0, latest - 250000)
        if from_block > latest:
            from_block = latest
        topic = "0x" + w3.keccak(
            text="ProposalCreated(uint256,address,address[],uint256[],string[],bytes[],uint256,uint256,string)"
        ).hex()
        if topic.startswith("0x0x"):
            topic = topic[2:]
        # Page in chunks (RPC range limits)
        cursor = from_block
        chunk = 5000
        while cursor <= latest:
            end = min(latest, cursor + chunk - 1)
            try:
                logs = w3.eth.get_logs({
                    "address": _dao_governor_address(),
                    "fromBlock": cursor,
                    "toBlock": end,
                    "topics": [topic],
                })
            except Exception as e:
                # Shrink chunk on range errors
                if chunk > 500:
                    chunk = max(500, chunk // 2)
                    continue
                stats["error"] = str(e)[:160]
                print(f"  [dao] get_logs fail {cursor}-{end}: {e}", flush=True)
                break
            for lg in logs:
                try:
                    ev = gov.events.ProposalCreated().process_log(lg)
                    args = ev["args"]
                    pid = int(args["proposalId"])
                    desc = args.get("description") or ""
                    title = (desc.split("\n")[0] if desc else "").strip() or f"Proposal {str(pid)[:6]}"
                    proposer_s = Web3_to_str_addr(args.get("proposer"))
                    state = int(gov.functions.state(pid).call())
                    against_w, for_w, abstain_w = gov.functions.proposalVotes(pid).call()
                    try:
                        snap = int(gov.functions.proposalSnapshot(pid).call())
                    except Exception:
                        snap = int(args.get("voteStart") or 0)
                    try:
                        deadline = int(gov.functions.proposalDeadline(pid).call())
                    except Exception:
                        deadline = int(args.get("voteEnd") or 0)
                    dao_upsert_proposal(conn, {
                        "proposal_id": str(pid),
                        "short_id": str(pid)[:6],
                        "proposer": proposer_s,
                        "title": title,
                        "description": desc,
                        "state": state,
                        "for_wei": str(for_w),
                        "against_wei": str(against_w),
                        "abstain_wei": str(abstain_w),
                        "snapshot_block": snap,
                        "deadline_block": deadline,
                    })
                    stats["new_logs"] += 1
                except Exception as e:
                    print(f"  [dao] decode/upsert fail: {e}", flush=True)
            dao_set_scan_value(conn, "last_scanned_block", str(end))
            cursor = end + 1

        # Refresh non-terminal cached proposals
        for r in dao_list_proposals(conn, active_only=False, limit=100):
            st = int(r.get("state") if r.get("state") is not None else -1)
            if st in DAO_TERMINAL_STATES:
                continue
            if dao_refresh_proposal_live(conn, str(r.get("proposal_id"))):
                stats["refreshed"] += 1
        dao_set_scan_value(conn, "last_poll_at", str(int(time.time())))
        print(f"  [dao] scan ok · new={stats['new_logs']} refreshed={stats['refreshed']} tip={latest}", flush=True)
    except Exception as e:
        stats["error"] = str(e)[:200]
        print(f"  [dao] scan error: {e}", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stats


def Web3_to_str_addr(addr) -> str:
    try:
        from web3 import Web3
        return Web3.to_checksum_address(addr)
    except Exception:
        return str(addr)


def start_dao_poller():
    """Background ~5min poller. Read-only; fail-open on errors."""
    global _DAO_POLL_THREAD
    with _DAO_POLL_LOCK:
        if _DAO_POLL_THREAD and _DAO_POLL_THREAD.is_alive():
            return

        def _loop():
            # Initial delay so boot isn't blocked
            time.sleep(8)
            while True:
                try:
                    dao_scan_and_refresh(get_db)
                except Exception as e:
                    print(f"  [dao] poller tick error: {e}", flush=True)
                try:
                    secs = int(os.environ.get("DAO_POLL_SECONDS", "300") or 300)
                except Exception:
                    secs = 300
                time.sleep(max(60, secs))

        _DAO_POLL_THREAD = threading.Thread(target=_loop, daemon=True, name="dao-poller")
        _DAO_POLL_THREAD.start()
        print("  [dao] poller started", flush=True)


# ── #ask-ai channel bot (AIVM + optional knowledge RAG) ───────────────────────
def _ask_ai_worker(get_db, socketio, wallet: str, content: str, display_name: str = "", slug: str = "ask-ai"):
    """Background: retrieve context → run_inference → post Lightchain AI reply."""
    from community import (
        ask_ai_rate_ok,
        format_chat_context_for_prompt,
        format_knowledge_for_prompt,
        format_dao_proposals_for_prompt,
        dao_question_looks_governance,
        dao_list_proposals,
        dao_find_proposal,
        dao_get_scan_value,
        post_channel_bot_message,
        search_knowledge,
        search_messages_for_context,
        AIVM_BOT_NAME,
    )

    wallet = (wallet or "").lower().strip()
    question = (content or "").strip()
    slug = (slug or "ask-ai").lower().strip() or "ask-ai"
    if not wallet or not question:
        return

    client = _get_aivm_client()
    conn = None
    try:
        conn = get_db()
        if not client:
            # Fail-closed: stay quiet (channel still works for chat)
            return
        if (os.environ.get("AIVM_ASK_BOOSTER_ONLY") or "").strip().lower() in ("1", "true", "yes"):
            # Boosting not shipped yet — deny clearly when flag flipped on
            post_channel_bot_message(
                conn, socketio, slug,
                "⚠️ Booster-only mode is on for #ask-ai right now.",
                AIVM_BOT_NAME,
            )
            return
        if not ask_ai_rate_ok(wallet):
            post_channel_bot_message(
                conn, socketio, slug,
                "⏳ Slow down — you've hit the per-hour Ask AI limit. Try again later.",
                AIVM_BOT_NAME,
            )
            return

        # Phase 1: chat history (never #mods — staff=False)
        chat_hits, _meta = search_messages_for_context(
            conn, question, limit=8, staff=False
        )
        chat_block = format_chat_context_for_prompt(chat_hits, max_chars=2800)

        # Phase 2: knowledge chunks (empty when KNOWLEDGE_SOURCES unset / not ingested)
        knowledge_hits = search_knowledge(conn, question, limit=5)
        knowledge_block = format_knowledge_for_prompt(knowledge_hits, max_chars=2200)

        # Live DAO proposals (read-only cache; fail-open if empty/stale)
        dao_block = ""
        if dao_question_looks_governance(question):
            try:
                # Prefer a specific match; else list active
                qlow = (question or "").lower()
                specific = dao_find_proposal(conn, question)
                want_one = bool(
                    specific
                    and (
                        re.search(r"#\d{4,}", question or "")
                        or "explain" in qlow
                        or "about" in qlow
                        or "how's" in qlow
                        or "how is" in qlow
                    )
                )
                if want_one and specific:
                    live = dao_refresh_proposal_live(conn, str(specific.get("proposal_id")))
                    row = live or specific
                    dao_block = format_dao_proposals_for_prompt([row], full=True, max_chars=3500)
                else:
                    active = dao_list_proposals(conn, active_only=True, limit=8)
                    if not active:
                        active = dao_list_proposals(conn, active_only=False, limit=8)
                    dao_block = format_dao_proposals_for_prompt(active, full=False, max_chars=2800)
                if not dao_block:
                    last = dao_get_scan_value(conn, "last_poll_at", "")
                    dao_block = (
                        "No cached governance proposals yet "
                        "(poller may still be warming up)."
                        + (f" last_poll={last}" if last else "")
                    )
            except Exception as e:
                print(f"  [ask-ai] dao context error: {e}", flush=True)
                dao_block = "Governance data temporarily unavailable — answer without inventing proposal details."

        session_id = f"ask-ai:{wallet}"
        conv = _get_conversation_context(session_id)

        prompt_parts = [
            "You are Lightchain AI, the assistant in this community's #ask-ai channel.",
            "Answer using the reference material below when relevant. If the docs cover it,",
            "explain clearly and mention which source. If you're unsure, say so — don't invent.",
            "For governance questions, use ONLY the on-chain proposal data provided — never invent tallies or titles.",
            "",
        ]
        if dao_block:
            prompt_parts.append("Reference — Lightchain governance (on-chain):")
            prompt_parts.append(dao_block)
            prompt_parts.append("")
        if knowledge_block:
            prompt_parts.append("Reference — Lightchain docs:")
            prompt_parts.append(knowledge_block)
            prompt_parts.append("")
        if chat_block:
            prompt_parts.append("Reference — relevant messages from this server:")
            prompt_parts.append(chat_block)
            prompt_parts.append("")
        if conv:
            prompt_parts.append("Conversation so far:")
            prompt_parts.append(conv)
            prompt_parts.append("")
        who = (display_name or "").strip() or wallet[:10]
        prompt_parts.append(f"User ({who}): {question}")
        prompt_parts.append("Assistant:")
        prompt = "\n".join(prompt_parts)

        try:
            answer = client.run_inference(prompt)
            if answer.lstrip().startswith("Assistant:"):
                answer = answer.lstrip()[len("Assistant:"):].lstrip()
            answer = (answer or "").strip()
            if not answer:
                answer = "⚠️ I couldn't answer that right now."
        except Exception as e:
            err = str(e)
            print(f"  [ask-ai] inference error: {err}", flush=True)
            low = err.lower()
            if "balance" in low or "reverted" in low or "submitjob" in low:
                answer = "⚠️ The AI's LCAI balance is empty — boost the server to refill it."
            else:
                answer = "⚠️ I couldn't answer that right now."

        # Cap bot reply length for chat readability
        if len(answer) > 3500:
            answer = answer[:3497] + "…"
        post_channel_bot_message(conn, socketio, slug, answer, AIVM_BOT_NAME)
        try:
            _save_to_session(session_id, question, answer)
        except Exception:
            pass
    except Exception as e:
        print(f"  [ask-ai] worker failed: {e}", flush=True)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


# ── Community layer (official Lightchain server, channels, roles, profiles) ──
try:
    from community import (
        register_community_routes,
        set_ask_ai_runner,
        set_push_sender,
        knowledge_sources_from_env,
    )
    register_community_routes(app, socketio, get_db)
    set_ask_ai_runner(_ask_ai_worker)
    set_push_sender(send_push_notification)
    _ks = knowledge_sources_from_env()
    print(f"  [ask-ai] runner registered · knowledge sources: {len(_ks)}")
    print("  [push] community mention/ticket sender registered")
    try:
        start_dao_poller()
    except Exception as _dao_err:
        print(f"  [dao] poller failed to start: {_dao_err}")
except Exception as _comm_err:
    print(f"  [community] FAILED to load: {_comm_err}")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
