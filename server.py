import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, join_room, emit
from flask_cors import CORS
import sqlite3
import os
import time
import re
import threading

app = Flask(__name__)
CORS(app, origins="*")
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'lightchat-secret-2026')
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
    ''')
    conn.commit()
    conn.close()

init_db()

def cleanup_messages():
    while True:
        time.sleep(60)
        try:
            conn = get_db()
            conn.execute('DELETE FROM messages WHERE expires_at < ?', (int(time.time()),))
            conn.commit()
            conn.close()
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

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'LightChat'})


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

# WebSocket handlers
@socketio.on('connect')
def on_connect():
    pass

@socketio.on('auth')
def on_auth(data):
    wallet = (data.get('wallet') or '').lower()
    if wallet:
        join_room(wallet)

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
    emit('notification', {
        'from_wallet': sender,
        'handle': get_handle_for(sender),
        'preview': content[:50]
    }, room=recipient)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port)
