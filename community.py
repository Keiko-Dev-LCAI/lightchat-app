"""
LightChat community layer — official server, channels, roles, profiles, events.
Wired from server.py. Brainstorm: ~/Desktop/LightChat/
"""
from __future__ import annotations

import json
import os
import time
import uuid
import base64
import re
from functools import wraps

# Official community id
COMMUNITY_ID = "lightchain-official"

DEFAULT_CHANNELS = [
    ("start-here", "Start Here", "welcome", "Welcome, rules, how to use LightChat", 0, 1),
    ("announcements", "Announcements", "info", "Official updates (mods post)", 1, 1),
    ("general", "Gen Chat", "text", "Main hangout — everyone talks here", 2, 0),
    ("introduce-yourself", "Introduce Yourself", "text", "New members say hi", 3, 0),
    ("help", "Help", "text", "Questions and support", 4, 0),
    ("dev", "Dev", "text", "Builders and technical talk", 5, 0),
    ("ai", "AI", "text", "AI and AIVM discussion", 6, 0),
    ("proposals", "Proposals", "text", "DAO and ideas", 7, 0),
    ("links", "Links", "info", "Official links and contracts", 8, 1),
    ("report", "Report", "text", "Report issues", 9, 0),
]

# Discord-style Start Here guide (shown in #start-here UI)
START_HERE_SECTIONS = [
    {
        "title": "Welcome to LightChat",
        "body": "Wallet-native community chat for Lightchain. Free for everyone — connect your wallet, set a display name, then join Gen Chat.",
        "links": [
            {"label": "Open Gen Chat", "href": "#channel:general", "kind": "channel"},
            {"label": "Edit Profile", "href": "#action:edit-profile", "kind": "action"},
        ],
    },
    {
        "title": "Learn about Lightchain",
        "body": "Official site, docs, and brand.",
        "links": [
            {"label": "lightchain.ai", "href": "https://lightchain.ai"},
            {"label": "Developer docs", "href": "https://docs.lightchain.ai/"},
            {"label": "Brand guidelines", "href": "https://lightchain.ai/brand"},
            {"label": "Whitepaper (PDF)", "href": "https://lightchain.ai/lightchain-whitepaper.pdf"},
            {"label": "Roadmap", "href": "https://lightchain.ai/roadmap"},
        ],
    },
    {
        "title": "Connect with the community",
        "body": "Talk with everyone, find members, introduce yourself.",
        "links": [
            {"label": "# Gen Chat", "href": "#channel:general", "kind": "channel"},
            {"label": "# Introduce Yourself", "href": "#channel:introduce-yourself", "kind": "channel"},
            {"label": "Directory", "href": "#action:directory", "kind": "action"},
            {"label": "# Help", "href": "#channel:help", "kind": "channel"},
        ],
    },
    {
        "title": "Community updates",
        "body": "Official news — mods post in Announcements.",
        "links": [
            {"label": "# Announcements", "href": "#channel:announcements", "kind": "channel"},
            {"label": "Events", "href": "#action:events", "kind": "action"},
            {"label": "Governance / DAO", "href": "https://dao.lightchain.ai/"},
        ],
    },
    {
        "title": "Network details",
        "body": "Lightchain L1 mainnet for wallets and apps.",
        "links": [
            {"label": "Chain ID 9200", "href": "https://mainnet.lightscan.app/"},
            {"label": "Block explorer (Lightscan)", "href": "https://mainnet.lightscan.app/"},
            {"label": "RPC https://rpc.mainnet.lightchain.ai", "href": "https://rpc.mainnet.lightchain.ai"},
            {"label": "Gas tracker", "href": "https://mainnet.lightscan.app/gas-tracker"},
        ],
    },
    {
        "title": "Lightchain links",
        "body": "Bridge, hub, faucet, workers.",
        "links": [
            {"label": "dApp Hub", "href": "https://hub.lightchain.ai/"},
            {"label": "Bridge", "href": "https://bridge.lightchain.ai/"},
            {"label": "Faucet", "href": "https://lightfaucet.ai/"},
            {"label": "Workers", "href": "https://workers.lightchain.ai/"},
            {"label": "Forum", "href": "https://forum.lightchain.ai/"},
        ],
    },
    {
        "title": "How LightChat works",
        "body": "Login with wallet only (English). Channels for topics. Friends for people you add. No LightPay — this community is free.",
        "links": [
            {"label": "# Links (bookmark shelf)", "href": "#channel:links", "kind": "channel"},
            {"label": "# Report", "href": "#channel:report", "kind": "channel"},
        ],
    },
]

# role rank: higher = more power
ROLE_RANK = {"owner": 100, "admin": 80, "mod": 50, "helper": 30, "member": 10}

# simple permission sets
ROLE_PERMS = {
    "owner": {
        "send_any", "delete_any", "timeout", "kick", "ban",
        "manage_channels", "manage_roles", "manage_invites",
        "create_events", "server_settings", "audit", "manage_owners",
        "post_announcements",
    },
    "admin": {
        "send_any", "delete_any", "timeout", "kick", "ban",
        "manage_channels", "manage_roles", "manage_invites",
        "create_events", "server_settings", "audit", "post_announcements",
    },
    "mod": {
        "send_any", "delete_any", "timeout", "kick",
        "manage_invites", "create_events", "post_announcements",
    },
    "helper": {"send_any", "delete_any"},
    "member": {"send_any"},
}


def _norm(w: str) -> str:
    return (w or "").lower().strip()


def init_community_db(get_db):
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS community_meta (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            icon TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS community_channels (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            description TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            readonly_members INTEGER NOT NULL DEFAULT 0,
            UNIQUE(community_id, slug)
        );
        CREATE TABLE IF NOT EXISTS community_members (
            community_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (community_id, wallet)
        );
        CREATE TABLE IF NOT EXISTS community_messages (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            sender_wallet TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS community_events (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            start_time INTEGER NOT NULL,
            end_time INTEGER,
            created_by TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS community_profiles (
            wallet TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            bio TEXT NOT NULL DEFAULT '',
            avatar_data TEXT,
            avatar_type TEXT,
            updated_at INTEGER NOT NULL
        );
        """
    )
    # seed community
    row = conn.execute(
        "SELECT id FROM community_meta WHERE id = ?", (COMMUNITY_ID,)
    ).fetchone()
    now = int(time.time())
    if not row:
        conn.execute(
            "INSERT INTO community_meta (id, name, description, created_at) VALUES (?,?,?,?)",
            (
                COMMUNITY_ID,
                "Lightchain",
                "Official Lightchain community on LightChat — wallet-native chat.",
                now,
            ),
        )
        for slug, name, kind, desc, sort, ro in DEFAULT_CHANNELS:
            cid = f"{COMMUNITY_ID}:{slug}"
            conn.execute(
                """INSERT INTO community_channels
                   (id, community_id, slug, name, kind, description, sort_order, readonly_members)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (cid, COMMUNITY_ID, slug, name, kind, desc, sort, ro),
            )
    # keep start-here / links read-only for members (migrate existing DBs)
    try:
        conn.execute(
            "UPDATE community_channels SET readonly_members=1 WHERE community_id=? AND slug IN ('start-here','links','announcements')",
            (COMMUNITY_ID,),
        )
        conn.commit()
    except Exception:
        pass

    # bootstrap owners from env (comma-separated, max 2)
    owners = [
        _norm(x)
        for x in os.environ.get("LIGHTCHAT_OWNER_WALLETS", "").split(",")
        if _norm(x).startswith("0x") and len(_norm(x)) == 42
    ][:2]
    for ow in owners:
        existing = conn.execute(
            "SELECT role FROM community_members WHERE community_id=? AND wallet=?",
            (COMMUNITY_ID, ow),
        ).fetchone()
        if existing:
            if existing["role"] != "owner":
                conn.execute(
                    "UPDATE community_members SET role=? WHERE community_id=? AND wallet=?",
                    ("owner", COMMUNITY_ID, ow),
                )
        else:
            conn.execute(
                "INSERT INTO community_members (community_id, wallet, role, joined_at) VALUES (?,?,?,?)",
                (COMMUNITY_ID, ow, "owner", now),
            )
    # profile columns on handles if useful — keep community_profiles separate
    conn.commit()
    conn.close()


def get_role(conn, wallet: str) -> str:
    wallet = _norm(wallet)
    row = conn.execute(
        "SELECT role FROM community_members WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, wallet),
    ).fetchone()
    return row["role"] if row else None


def ensure_member(conn, wallet: str) -> str:
    """Join as member if not in community. First human join becomes owner if no owners."""
    wallet = _norm(wallet)
    if not wallet.startswith("0x"):
        return None
    role = get_role(conn, wallet)
    if role:
        return role
    now = int(time.time())
    n_owners = conn.execute(
        "SELECT COUNT(*) AS c FROM community_members WHERE community_id=? AND role='owner'",
        (COMMUNITY_ID,),
    ).fetchone()["c"]
    role = "owner" if n_owners == 0 else "member"
    conn.execute(
        "INSERT INTO community_members (community_id, wallet, role, joined_at) VALUES (?,?,?,?)",
        (COMMUNITY_ID, wallet, role, now),
    )
    conn.commit()
    return role


def has_perm(role: str, perm: str) -> bool:
    if not role:
        return False
    return perm in ROLE_PERMS.get(role, set())


def rank(role: str) -> int:
    return ROLE_RANK.get(role or "", 0)


def profile_dict(conn, wallet: str) -> dict:
    wallet = _norm(wallet)
    p = conn.execute(
        "SELECT display_name, bio, avatar_type, updated_at FROM community_profiles WHERE wallet=?",
        (wallet,),
    ).fetchone()
    h = conn.execute("SELECT handle FROM handles WHERE wallet=?", (wallet,)).fetchone()
    role = get_role(conn, wallet)
    display = ""
    bio = ""
    has_avatar = False
    if p:
        display = p["display_name"] or ""
        bio = p["bio"] or ""
        has_avatar = bool(
            conn.execute(
                "SELECT 1 FROM community_profiles WHERE wallet=? AND avatar_data IS NOT NULL AND avatar_data != ''",
                (wallet,),
            ).fetchone()
        )
    handle = h["handle"] if h else ""
    if not display:
        display = handle or (wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet)
    return {
        "wallet": wallet,
        "display_name": display,
        "bio": bio,
        "handle": handle,
        "role": role or "member",
        "has_avatar": has_avatar,
        "avatar_url": f"/api/community/avatar/{wallet}" if has_avatar else None,
    }


def register_community_routes(app, socketio, get_db):
    from flask import request, jsonify, Response
    from flask_socketio import join_room

    init_community_db(get_db)

    @app.route("/api/community")
    def api_community():
        conn = get_db()
        try:
            meta = conn.execute(
                "SELECT * FROM community_meta WHERE id=?", (COMMUNITY_ID,)
            ).fetchone()
            channels = conn.execute(
                """SELECT id, slug, name, kind, description, sort_order, readonly_members
                   FROM community_channels WHERE community_id=? ORDER BY sort_order""",
                (COMMUNITY_ID,),
            ).fetchall()
            n_members = conn.execute(
                "SELECT COUNT(*) AS c FROM community_members WHERE community_id=?",
                (COMMUNITY_ID,),
            ).fetchone()["c"]
            return jsonify({
                "id": COMMUNITY_ID,
                "name": meta["name"] if meta else "Lightchain",
                "description": meta["description"] if meta else "",
                "member_count": n_members,
                "channels": [dict(c) for c in channels],
                "start_here": START_HERE_SECTIONS,
                "brand": {
                    "primary": "#5B4BFF",
                    "secondary": "#DD00AC",
                    "dark": "#14152C",
                    "light": "#CCCEEF",
                    "gradient_end": "#EE11FB",
                },
            })
        finally:
            conn.close()

    @app.route("/api/community/start-here")
    def api_start_here():
        return jsonify({
            "slug": "start-here",
            "title": "Start Here",
            "subtitle": "Welcome to the Lightchain community on LightChat",
            "sections": START_HERE_SECTIONS,
        })

    @app.route("/api/community/join", methods=["POST"])
    def api_community_join():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        if not wallet.startswith("0x") or len(wallet) != 42:
            return jsonify({"error": "valid wallet required"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            return jsonify({"ok": True, "role": role, "profile": profile_dict(conn, wallet)})
        finally:
            conn.close()
    @app.route("/api/community/me")
    def api_community_me():
        wallet = _norm(request.args.get("wallet", ""))
        if not wallet:
            return jsonify({"error": "wallet required"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            return jsonify({"role": role, "profile": profile_dict(conn, wallet), "perms": list(ROLE_PERMS.get(role, []))})
        finally:
            conn.close()

    @app.route("/api/community/directory")
    def api_directory():
        q = (request.args.get("q") or "").strip().lower()
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT wallet, role, joined_at FROM community_members
                   WHERE community_id=? ORDER BY joined_at DESC LIMIT 200""",
                (COMMUNITY_ID,),
            ).fetchall()
            out = []
            for r in rows:
                prof = profile_dict(conn, r["wallet"])
                prof["joined_at"] = r["joined_at"]
                if q:
                    blob = (prof["display_name"] + prof["handle"] + prof["wallet"]).lower()
                    if q not in blob:
                        continue
                out.append(prof)
            return jsonify({"members": out, "count": len(out)})
        finally:
            conn.close()

    @app.route("/api/community/profile", methods=["POST"])
    def api_profile_update():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        display = (data.get("display_name") or "")[:40].strip()
        bio = (data.get("bio") or "")[:280].strip()
        now = int(time.time())
        conn = get_db()
        try:
            ensure_member(conn, wallet)
            existing = conn.execute(
                "SELECT wallet FROM community_profiles WHERE wallet=?", (wallet,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE community_profiles SET display_name=?, bio=?, updated_at=? WHERE wallet=?",
                    (display, bio, now, wallet),
                )
            else:
                conn.execute(
                    """INSERT INTO community_profiles (wallet, display_name, bio, updated_at)
                       VALUES (?,?,?,?)""",
                    (wallet, display, bio, now),
                )
            conn.commit()
            return jsonify({"ok": True, "profile": profile_dict(conn, wallet)})
        finally:
            conn.close()

    @app.route("/api/community/avatar", methods=["POST"])
    def api_avatar_upload():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        raw = data.get("image_data") or ""
        itype = (data.get("image_type") or "image/jpeg")[:40]
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not raw or len(raw) > 2_500_000:
            return jsonify({"error": "image too large or missing (max ~2MB)"}), 400
        # strip data URL prefix
        if "," in raw[:80]:
            raw = raw.split(",", 1)[1]
        now = int(time.time())
        conn = get_db()
        try:
            ensure_member(conn, wallet)
            existing = conn.execute(
                "SELECT wallet FROM community_profiles WHERE wallet=?", (wallet,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE community_profiles SET avatar_data=?, avatar_type=?, updated_at=? WHERE wallet=?",
                    (raw, itype, now, wallet),
                )
            else:
                conn.execute(
                    """INSERT INTO community_profiles
                       (wallet, display_name, bio, avatar_data, avatar_type, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (wallet, "", "", raw, itype, now),
                )
            conn.commit()
            return jsonify({"ok": True, "avatar_url": f"/api/community/avatar/{wallet}"})
        finally:
            conn.close()

    @app.route("/api/community/avatar/<wallet>")
    def api_avatar_get(wallet):
        wallet = _norm(wallet)
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT avatar_data, avatar_type FROM community_profiles WHERE wallet=?",
                (wallet,),
            ).fetchone()
            if not row or not row["avatar_data"]:
                return jsonify({"error": "no avatar"}), 404
            try:
                body = base64.b64decode(row["avatar_data"])
            except Exception:
                return jsonify({"error": "bad avatar"}), 500
            return Response(body, mimetype=row["avatar_type"] or "image/jpeg")
        finally:
            conn.close()

    @app.route("/api/community/messages/<slug>")
    def api_channel_messages(slug):
        limit = min(int(request.args.get("limit", 80)), 200)
        conn = get_db()
        try:
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            rows = conn.execute(
                """SELECT id, sender_wallet, content, created_at FROM community_messages
                   WHERE channel_id=? ORDER BY created_at DESC LIMIT ?""",
                (ch["id"], limit),
            ).fetchall()
            msgs = []
            for r in reversed(list(rows)):
                prof = profile_dict(conn, r["sender_wallet"])
                msgs.append(
                    {
                        "id": r["id"],
                        "sender_wallet": r["sender_wallet"],
                        "content": r["content"],
                        "created_at": r["created_at"],
                        "display_name": prof["display_name"],
                        "role": prof["role"],
                        "has_avatar": prof["has_avatar"],
                    }
                )
            return jsonify({"messages": msgs, "channel_id": ch["id"], "slug": slug})
        finally:
            conn.close()

    @app.route("/api/community/messages/<slug>", methods=["POST"])
    def api_channel_send(slug):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        content = (data.get("content") or "").strip()
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not content or len(content) > 4000:
            return jsonify({"error": "content required (max 4000)"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            ch = conn.execute(
                "SELECT * FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            if ch["readonly_members"] and not has_perm(role, "post_announcements"):
                if role not in ("owner", "admin", "mod"):
                    return jsonify({"error": "only mods can post here"}), 403
            mid = str(uuid.uuid4())
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_messages
                   (id, community_id, channel_id, sender_wallet, content, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (mid, COMMUNITY_ID, ch["id"], wallet, content, now),
            )
            conn.commit()
            prof = profile_dict(conn, wallet)
            msg = {
                "id": mid,
                "sender_wallet": wallet,
                "content": content,
                "created_at": now,
                "display_name": prof["display_name"],
                "role": prof["role"],
                "has_avatar": prof["has_avatar"],
                "slug": slug,
            }
            try:
                socketio.emit("community_message", msg, room=f"community:{slug}")
            except Exception:
                pass
            return jsonify({"ok": True, "message": msg})
        finally:
            conn.close()

    @app.route("/api/community/events")
    def api_events_list():
        conn = get_db()
        try:
            now = int(time.time())
            rows = conn.execute(
                """SELECT * FROM community_events WHERE community_id=? AND start_time >= ?
                   ORDER BY start_time ASC LIMIT 50""",
                (COMMUNITY_ID, now - 86400),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["creator"] = profile_dict(conn, r["created_by"])
                out.append(d)
            return jsonify({"events": out})
        finally:
            conn.close()

    @app.route("/api/community/events", methods=["POST"])
    def api_events_create():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        title = (data.get("title") or "").strip()[:120]
        if not wallet or not title:
            return jsonify({"error": "wallet and title required"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if not has_perm(role, "create_events") and role not in ("owner", "admin", "mod"):
                return jsonify({"error": "mods only"}), 403
            eid = str(uuid.uuid4())
            now = int(time.time())
            start = int(data.get("start_time") or now + 3600)
            end = int(data["end_time"]) if data.get("end_time") else None
            desc = (data.get("description") or "")[:1000]
            conn.execute(
                """INSERT INTO community_events
                   (id, community_id, title, description, start_time, end_time, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (eid, COMMUNITY_ID, title, desc, start, end, wallet, now),
            )
            conn.commit()
            return jsonify({"ok": True, "id": eid})
        finally:
            conn.close()

    @app.route("/api/community/roles", methods=["POST"])
    def api_set_role():
        data = request.json or {}
        actor = _norm(data.get("wallet", ""))
        target = _norm(data.get("target", ""))
        new_role = (data.get("role") or "").lower().strip()
        if new_role not in ROLE_RANK:
            return jsonify({"error": "invalid role"}), 400
        conn = get_db()
        try:
            a_role = ensure_member(conn, actor)
            t_role = get_role(conn, target) or "member"
            if not has_perm(a_role, "manage_roles") and a_role != "owner":
                return jsonify({"error": "forbidden"}), 403
            if new_role == "owner":
                if not has_perm(a_role, "manage_owners") and a_role != "owner":
                    return jsonify({"error": "only owners can assign owner"}), 403
                n_owners = conn.execute(
                    "SELECT COUNT(*) AS c FROM community_members WHERE community_id=? AND role='owner'",
                    (COMMUNITY_ID,),
                ).fetchone()["c"]
                if t_role != "owner" and n_owners >= 2:
                    return jsonify({"error": "max 2 owners"}), 400
            if rank(a_role) <= rank(t_role) and actor != target:
                if a_role != "owner":
                    return jsonify({"error": "cannot change equal or higher role"}), 403
            if not get_role(conn, target):
                ensure_member(conn, target)
            conn.execute(
                "UPDATE community_members SET role=? WHERE community_id=? AND wallet=?",
                (new_role, COMMUNITY_ID, target),
            )
            # never zero owners
            n_owners = conn.execute(
                "SELECT COUNT(*) AS c FROM community_members WHERE community_id=? AND role='owner'",
                (COMMUNITY_ID,),
            ).fetchone()["c"]
            if n_owners < 1:
                conn.execute(
                    "UPDATE community_members SET role='owner' WHERE community_id=? AND wallet=?",
                    (COMMUNITY_ID, actor),
                )
                conn.commit()
                return jsonify({"error": "must keep at least one owner"}), 400
            conn.commit()
            return jsonify({"ok": True, "target": target, "role": new_role})
        finally:
            conn.close()

    @app.route("/api/community/owners", methods=["POST"])
    def api_owners():
        """Add or remove co-owner. Body: wallet (actor), action add|remove, target."""
        data = request.json or {}
        actor = _norm(data.get("wallet", ""))
        target = _norm(data.get("target", ""))
        action = (data.get("action") or "").lower()
        conn = get_db()
        try:
            a_role = ensure_member(conn, actor)
            if a_role != "owner":
                return jsonify({"error": "owners only"}), 403
            if action == "add":
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM community_members WHERE community_id=? AND role='owner'",
                    (COMMUNITY_ID,),
                ).fetchone()["c"]
                if n >= 2:
                    return jsonify({"error": "max 2 owners"}), 400
                ensure_member(conn, target)
                conn.execute(
                    "UPDATE community_members SET role='owner' WHERE community_id=? AND wallet=?",
                    (COMMUNITY_ID, target),
                )
            elif action == "remove":
                if target == actor:
                    # allow remove self only if other owner exists
                    n = conn.execute(
                        "SELECT COUNT(*) AS c FROM community_members WHERE community_id=? AND role='owner'",
                        (COMMUNITY_ID,),
                    ).fetchone()["c"]
                    if n < 2:
                        return jsonify({"error": "add another owner first"}), 400
                conn.execute(
                    "UPDATE community_members SET role='admin' WHERE community_id=? AND wallet=? AND role='owner'",
                    (COMMUNITY_ID, target),
                )
            else:
                return jsonify({"error": "action add|remove"}), 400
            conn.commit()
            owners = conn.execute(
                "SELECT wallet FROM community_members WHERE community_id=? AND role='owner'",
                (COMMUNITY_ID,),
            ).fetchall()
            return jsonify({"ok": True, "owners": [o["wallet"] for o in owners]})
        finally:
            conn.close()

    @socketio.on("join_community_channel")
    def on_join_community(data):
        slug = (data or {}).get("slug") or "general"
        join_room(f"community:{slug}")

    print("  [community] routes registered — official Lightchain server ready")
