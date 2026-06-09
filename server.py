import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, make_response, send_from_directory
from flask_socketio import SocketIO, join_room, emit
from flask_cors import CORS
import sqlite3
import os
import time
import re
import threading
import json
import base64
import uuid

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False

VAPID_PUBLIC_KEY = "BARwCZXpnFLr_5wN2AtVFC0TE6_wfOiuq8jS6Gxvf-qt3R0QLUCkxXbQr7a1JYI_MqZmU0JfuLXmPPu8e85lFlI"
VAPID_PRIVATE_KEY = "tzOJiS8pApHcxCBapYWKk63xle6Zia-Q1Gn4lAGaFsM"
VAPID_CLAIMS = {"sub": "mailto:noreply@lightchat.app"}

app = Flask(__name__)
CORS(app, origins="*")
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'lightchat-secret-2026')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max request
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

_data_dir = os.environ.get('DATA_DIR', '/app/data')
os.makedirs(_data_dir, exist_ok=True)
DB_PATH = os.environ.get('DB_PATH', os.path.join(_data_dir, 'lightchat.db'))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    ''')
    conn.commit()
    conn.close()

init_db()

# Migrate memories table: add media_type column if it doesn't exist yet
try:
    _mig_conn = get_db()
    _mig_conn.execute('ALTER TABLE memories ADD COLUMN media_type TEXT NOT NULL DEFAULT "image"')
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

def get_room(w1, w2):
    return '_'.join(sorted([w1.lower(), w2.lower()]))

def get_handle_for(wallet, conn=None):
    close = conn is None
    if close:
        conn = get_db()
    row = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (wallet.lower(),)).fetchone()
    if close:
        conn.close()
    return row['handle'] if row else wallet[:8] + '...'

def send_push_notification(to_wallet, title, body):
    if not PUSH_AVAILABLE:
        return
    try:
        conn = get_db()
        rows = conn.execute(
            'SELECT subscription FROM push_subscriptions WHERE wallet = ?',
            (to_wallet.lower(),)
        ).fetchall()
        conn.close()
        payload = json.dumps({'title': title, 'body': body})
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

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'LightChat'})

STICKERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stickers')

@app.route('/stickers/<path:filename>')
def serve_sticker(filename):
    return send_from_directory(STICKERS_DIR, filename)

@app.route('/vapid-public-key')
def vapid_public_key():
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})

@app.route('/subscribe', methods=['POST'])
def subscribe():
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

    handle = '@' + handle.lower()
    conn = get_db()
    try:
        existing = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (wallet,)).fetchone()
        if existing:
            return jsonify({'handle': existing['handle'], 'exists': True})

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
    return jsonify({'handle': row['handle'] if row else None})

@app.route('/resolve/<handle>')
def resolve_handle(handle):
    h = ('@' + handle.lstrip('@')).lower()
    conn = get_db()
    row = conn.execute('SELECT wallet FROM handles WHERE handle = ?', (h,)).fetchone()
    conn.close()
    if row:
        return jsonify({'wallet': row['wallet']})
    return jsonify({'wallet': None}), 404

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

        # Insert pending request: contact_wallet receives the request
        conn.execute(
            'INSERT OR IGNORE INTO contacts (wallet, contact_wallet, status, created_at) VALUES (?, ?, ?, ?)',
            (contact_wallet, wallet, 'pending', int(time.time()))
        )
        conn.commit()

        # Notify recipient if online
        socketio.emit('contact_request', {
            'from_wallet': wallet,
            'handle': get_handle_for(wallet, conn)
        }, room=contact_wallet)

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

    handle = '@' + handle.lower()
    conn = get_db()
    try:
        existing = conn.execute('SELECT wallet FROM handles WHERE handle = ?', (handle,)).fetchone()
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

    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    if not caption and not image_data:
        return jsonify({'error': 'caption or image required'}), 400

    now = int(time.time())
    expires_at = now + (90 * 24 * 60 * 60) if storage_type == 'cloud' else None

    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO memories (wallet, caption, image_data, image_type, storage_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (wallet, caption, image_data or None, image_type or None, storage_type, now, expires_at)
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
               COALESCE(m.media_type, 'image') as media_type
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
    now = int(time.time())
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO chat_images (wallet, image_data, image_type, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
        (wallet, image_data, image_type, now, now + 86400)
    )
    image_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'image_id': image_id})

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
from urllib.parse import urlparse as _urlparse

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

@app.route('/proxy-gif')
def proxy_gif():
    url = request.args.get('url', '')
    name = request.args.get('name', 'image.gif')
    if not url:
        return jsonify({'error': 'no url'}), 400
    # Only allow Tenor domains
    try:
        host = _urlparse(url).hostname or ''
    except Exception:
        host = ''
    if not (host.endswith('tenor.com') or host.endswith('tenor.co') or 'tenor' in host):
        return jsonify({'error': 'domain not allowed'}), 403
    try:
        req = _urllib_req.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _urllib_req.urlopen(req, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get('Content-Type', 'image/gif')
        resp = make_response(data)
        resp.headers['Content-Type'] = content_type
        safe_name = name.replace('"', '\\"')
        resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/chat-file', methods=['POST'])
def post_chat_file():
    data = request.json or {}
    wallet = data.get('wallet', '').lower().strip()
    file_name = data.get('file_name', 'file')
    file_data = data.get('file_data', '')
    file_type = data.get('file_type', 'application/octet-stream')
    file_size = data.get('file_size', 0)
    if not wallet or not file_data:
        return jsonify({'error': 'wallet and file_data required'}), 400
    now = int(time.time())
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO chat_files (wallet, file_name, file_data, file_type, file_size, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (wallet, file_name, file_data, file_type, file_size, now, now + 86400)
    )
    file_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'file_id': file_id})

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
    safe_name = row['file_name'].replace('"', '\\"')
    resp.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return resp

@app.route('/chat-voice', methods=['POST'])
def post_chat_voice():
    audio_data = request.data
    content_type = request.headers.get('Content-Type', 'audio/webm')
    if not audio_data:
        return jsonify({'error': 'no audio data'}), 400
    voice_id = str(uuid.uuid4())
    expires_at = int(time.time()) + 86400  # 24-hour TTL
    with _voice_lock:
        _voice_store[voice_id] = {
            'data': audio_data,
            'content_type': content_type,
            'expires_at': expires_at
        }
    return jsonify({'url': '/voice/' + voice_id})

@app.route('/voice/<voice_id>')
def get_chat_voice(voice_id):
    with _voice_lock:
        entry = _voice_store.get(voice_id)
    if not entry or entry['expires_at'] < int(time.time()):
        return jsonify({'error': 'not found'}), 404
    resp = make_response(entry['data'])
    resp.headers['Content-Type'] = entry['content_type']
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

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

@socketio.on('auth')
def on_auth(data):
    wallet = (data.get('wallet') or '').lower()
    if wallet:
        join_room(wallet)
        # Deliver any buffered call offer — handles iOS reconnect after being backgrounded
        pending = pending_calls.get(wallet)
        if pending:
            emit('call_offer', {
                'caller_wallet': pending['caller_wallet'],
                'handle': pending['handle'],
                'offer': pending['offer']
            })

@socketio.on('join_chat')
def on_join_chat(data):
    w1 = (data.get('wallet') or '').lower()
    w2 = (data.get('contact_wallet') or '').lower()
    if w1 and w2:
        join_room(get_room(w1, w2))

@socketio.on('send_message')
def on_send_message(data):
    sender = (data.get('sender_wallet') or '').lower()
    recipient = (data.get('recipient_wallet') or '').lower()
    content = (data.get('content') or '').strip()
    msg_type = data.get('type', 'text')

    if not sender or not recipient or not content:
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
    expires_at = now + (24 * 60 * 60)

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
    caller = (data.get('caller_wallet') or '').lower()
    callee = (data.get('callee_wallet') or '').lower()
    offer = data.get('offer')
    if not caller or not callee or not offer:
        return
    caller_handle = get_handle_for(caller)
    call_data = {'caller_wallet': caller, 'handle': caller_handle, 'offer': offer}
    # Buffer for 90s so if callee is backgrounded, they get the call when they reconnect
    pending_calls[callee] = call_data
    eventlet.spawn(_expire_pending_call, callee)
    # Emit to callee room (works if they're currently connected)
    emit('call_offer', call_data, room=callee)
    # Push notification to wake up callee if their tab is backgrounded
    eventlet.spawn(send_push_notification, callee, caller_handle, '📞 Incoming video call from ' + caller_handle)

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
    creator = (data.get('wallet') or '').lower()
    name = (data.get('name') or '').strip()
    members = data.get('members') or []
    if not creator or not name:
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
    sender = (data.get('wallet') or '').lower()
    group_id = (data.get('group_id') or '').strip()
    content = str(data.get('content') or '').strip()
    msg_type = str(data.get('type') or 'text')
    if not sender or not group_id or not content:
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
    expires_at = now + 86400
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
# PREMIUM TIER — LCAI price, call access, subscriptions, gift confirm
# ══════════════════════════════════════════════════════════════════════

OWNER_WALLET = '0x6518fd07b3da01b17bd37d7c40f9a5e3c87a09ba'
FREE_CALLS_LIMIT = 5

# Handles that always get premium access (testing / internal accounts)
PREMIUM_WHITELIST = {'@keiko326', '@bin1977'}

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
    w = wallet.lower().strip()
    now = int(time.time())
    conn = get_db()
    # Check whitelist first
    handle_row = conn.execute('SELECT handle FROM handles WHERE wallet = ?', (w,)).fetchone()
    if handle_row and handle_row['handle'] in PREMIUM_WHITELIST:
        conn.close()
        return jsonify({'allowed': True, 'free_remaining': 99, 'subscribed': True, 'expires_at': None})
    sub = conn.execute('SELECT expires_at FROM subscriptions WHERE wallet = ?', (w,)).fetchone()
    subscribed = bool(sub and sub['expires_at'] and sub['expires_at'] > now)
    expires_at = sub['expires_at'] if sub else None
    usage = conn.execute('SELECT free_calls_used FROM call_usage WHERE wallet = ?', (w,)).fetchone()
    free_used = usage['free_calls_used'] if usage else 0
    conn.close()
    free_remaining = max(0, FREE_CALLS_LIMIT - free_used)
    allowed = subscribed or (free_used < FREE_CALLS_LIMIT)
    return jsonify({
        'allowed': allowed,
        'free_remaining': free_remaining,
        'subscribed': subscribed,
        'expires_at': expires_at
    })


@app.route('/api/use-call', methods=['POST'])
def api_use_call():
    data = request.json or {}
    w = data.get('wallet', '').lower().strip()
    if not w:
        return jsonify({'error': 'wallet required'}), 400
    now = int(time.time())
    conn = get_db()
    sub = conn.execute('SELECT expires_at FROM subscriptions WHERE wallet = ?', (w,)).fetchone()
    subscribed = bool(sub and sub['expires_at'] and sub['expires_at'] > now)
    if not subscribed:
        conn.execute(
            'INSERT INTO call_usage (wallet, free_calls_used) VALUES (?, 1) '
            'ON CONFLICT(wallet) DO UPDATE SET free_calls_used = free_calls_used + 1',
            (w,)
        )
        conn.commit()
    usage = conn.execute('SELECT free_calls_used FROM call_usage WHERE wallet = ?', (w,)).fetchone()
    free_used = usage['free_calls_used'] if usage else 0
    conn.close()
    return jsonify({
        'allowed': subscribed or (free_used < FREE_CALLS_LIMIT),
        'free_remaining': max(0, FREE_CALLS_LIMIT - free_used),
        'subscribed': subscribed,
        'expires_at': sub['expires_at'] if sub else None
    })


@app.route('/api/verify-subscription', methods=['POST'])
def api_verify_subscription():
    data = request.json or {}
    w = data.get('wallet', '').lower().strip()
    tx_hash = (data.get('tx_hash') or '').strip()
    if not w or not tx_hash:
        return jsonify({'error': 'wallet and tx_hash required'}), 400
    # Whitelist bypass
    conn_wl = get_db()
    handle_wl = conn_wl.execute('SELECT handle FROM handles WHERE wallet = ?', (w,)).fetchone()
    conn_wl.close()
    if handle_wl and handle_wl['handle'] in PREMIUM_WHITELIST:
        return jsonify({'success': True, 'isPremium': True, 'expires_at': None, 'whitelisted': True})
    try:
        result = lightchain_rpc('eth_getTransactionByHash', [tx_hash])
        tx = result.get('result')
        if not tx:
            return jsonify({'error': 'Transaction not found on Lightchain — check the hash and try again'}), 404
        # Verify recipient is the owner wallet
        to_addr = (tx.get('to') or '').lower()
        if to_addr != OWNER_WALLET:
            return jsonify({'error': 'This transaction was not sent to the LightChat subscription address'}), 400
        # Verify value: must be >= $1 worth of LCAI
        price = get_lcai_price()
        required_lcai = 1.0 / price
        required_wei = int(required_lcai * 1e18)
        tx_value = int(tx.get('value', '0x0'), 16)
        if tx_value < required_wei:
            sent_lcai = round(tx_value / 1e18, 4)
            needed_lcai = round(required_lcai, 2)
            return jsonify({
                'error': f'Insufficient amount — sent {sent_lcai} LCAI, need {needed_lcai} LCAI'
            }), 400
        # Verify receipt status (soft check — receipt may not exist yet on very new tx)
        try:
            rcpt_result = lightchain_rpc('eth_getTransactionReceipt', [tx_hash]).get('result')
            if rcpt_result and rcpt_result.get('status') == '0x0':
                return jsonify({'error': 'Transaction was reverted — please send a new one'}), 400
        except Exception:
            pass  # Receipt may not exist yet; proceed with tx-existence check
        # Store 30-day subscription
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        conn = get_db()
        conn.execute(
            'INSERT OR REPLACE INTO subscriptions (wallet, expires_at, tx_hash) VALUES (?, ?, ?)',
            (w, expires_at, tx_hash)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'expires_at': expires_at})
    except Exception as e:
        return jsonify({'error': 'Could not verify transaction: ' + str(e)}), 500


@app.route('/api/confirm-gift', methods=['POST'])
def api_confirm_gift():
    data = request.json or {}
    from_wallet = (data.get('from_wallet') or '').lower().strip()
    to_wallet   = (data.get('to_wallet') or '').lower().strip()
    tx_hash     = (data.get('tx_hash') or '').strip()
    amount      = float(data.get('amount') or 0)
    if not from_wallet or not to_wallet or not tx_hash:
        return jsonify({'error': 'from_wallet, to_wallet, and tx_hash required'}), 400
    try:
        result = lightchain_rpc('eth_getTransactionByHash', [tx_hash])
        tx = result.get('result')
        if not tx:
            return jsonify({'error': 'Transaction not found on Lightchain — check the hash'}), 404
        tx_from = (tx.get('from') or '').lower()
        tx_to   = (tx.get('to')   or '').lower()
        if tx_to != to_wallet:
            return jsonify({'error': 'Transaction recipient does not match'}), 400
        if tx_from != from_wallet:
            return jsonify({'error': 'Transaction sender does not match your connected wallet'}), 400
        if amount > 0:
            expected_wei = int(amount * 1e18)
            tx_value = int(tx.get('value', '0x0'), 16)
            if tx_value < int(expected_wei * 0.99):  # 1% tolerance
                return jsonify({'error': 'Transaction amount is less than the gift amount'}), 400
        # Notify recipient via their personal socket room
        gift_content = json.dumps({'amount': amount, 'txHash': tx_hash})
        from_handle  = get_handle_for(from_wallet)
        socketio.emit('gift_confirmed', {
            'from_wallet': from_wallet,
            'handle':      from_handle,
            'content':     gift_content,
            'type':        'lcai_gift'
        }, room=to_wallet)
        return jsonify({'success': True, 'tx_hash': tx_hash})
    except Exception as e:
        return jsonify({'error': 'Could not verify transaction: ' + str(e)}), 500


_TURN_FALLBACK = [
    {'urls': 'stun:stun.l.google.com:19302'},
    {'urls': 'stun:stun1.l.google.com:19302'},
    {'urls': 'turn:openrelay.metered.ca:80',               'username': 'openrelayproject', 'credential': 'openrelayproject'},
    {'urls': 'turn:openrelay.metered.ca:443',              'username': 'openrelayproject', 'credential': 'openrelayproject'},
    {'urls': 'turn:openrelay.metered.ca:443?transport=tcp','username': 'openrelayproject', 'credential': 'openrelayproject'},
]

@app.route('/api/turn-credentials')
def api_turn_credentials():
    api_key = os.environ.get('METERED_API_KEY', '')
    if not api_key or not _REQUESTS_AVAILABLE:
        return jsonify(_TURN_FALLBACK)
    try:
        resp = _requests.get(
            f'https://lightchat.metered.live/api/v1/turn/credentials?apiKey={api_key}',
            timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception:
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
