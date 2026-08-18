"""
LightChat community layer — official server, channels, roles, profiles, events.
Wired from server.py. Brainstorm: ~/Desktop/LightChat/
"""
from __future__ import annotations

import json
import os
import random
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
    ("dev", "Devs", "text", "Builders, apps, contracts, tooling — P2P live chat", 5, 0),
    ("nodes", "Nodes", "text", "Node operators, validators, RPC, sync, hardware — P2P live chat", 6, 0),
    ("ai", "AI", "text", "AI and AIVM discussion", 7, 0),
    ("proposals", "Proposals", "text", "DAO and ideas", 8, 0),
    ("mods", "Mods", "text", "Staff only — mod issues & coordination · P2P", 9, 0),
    ("links", "Links", "info", "Official links and contracts", 10, 1),
    ("report", "Report", "text", "Report issues", 11, 0),
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
            {"label": "# Devs", "href": "#channel:dev", "kind": "channel"},
            {"label": "# Nodes", "href": "#channel:nodes", "kind": "channel"},
            {"label": "# Introduce Yourself", "href": "#channel:introduce-yourself", "kind": "channel"},
            {"label": "Members", "href": "#action:directory", "kind": "action"},
            {"label": "Open a Help ticket", "href": "#action:tickets", "kind": "action"},
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
    {
        "title": "⚠️ Stay safe — official help only",
        "body": "Real LightChat help is ONLY through Help tickets (sidebar → Help). Mods and admins will NEVER ask for your seed phrase, private key, or to “connect wallet” / sign a mystery transaction. Ignore friend requests or DMs that push airdrops, claims, or wallet connects. Scammers copy staff names — verify in Members (Admin/Mod role) and use tickets.",
        "links": [
            {"label": "Open a Help ticket", "href": "#action:tickets", "kind": "action"},
            {"label": "# Report", "href": "#channel:report", "kind": "channel"},
            {"label": "Members list", "href": "#action:directory", "kind": "action"},
        ],
    },
]

# role rank: higher = more power (Discord-style member list order)
ROLE_RANK = {"owner": 100, "admin": 80, "mod": 50, "helper": 30, "member": 10}

# Live presence: wallet -> {"mode": "online"|"invisible", "sid": socket id}
# Invisible = connected but appear offline to others (Discord-style).
PRESENCE = {}


def presence_get_mode(conn, wallet: str) -> str:
    wallet = _norm(wallet)
    try:
        row = conn.execute(
            "SELECT presence_mode FROM community_profiles WHERE wallet=?", (wallet,)
        ).fetchone()
        if row and row["presence_mode"] in ("online", "invisible"):
            return row["presence_mode"]
    except Exception:
        pass
    return "online"


def presence_set(wallet: str, mode: str, sid: str | None = None) -> dict:
    wallet = _norm(wallet)
    mode = mode if mode in ("online", "invisible") else "online"
    prev = PRESENCE.get(wallet) or {}
    entry = {"mode": mode, "sid": sid if sid is not None else prev.get("sid")}
    PRESENCE[wallet] = entry
    return entry


def presence_clear(wallet: str, sid: str | None = None) -> bool:
    """Remove presence. If sid given, only clear when it matches (multi-tab safe-ish)."""
    wallet = _norm(wallet)
    cur = PRESENCE.get(wallet)
    if not cur:
        return False
    if sid is not None and cur.get("sid") and cur.get("sid") != sid:
        return False
    PRESENCE.pop(wallet, None)
    return True


def presence_is_visible_online(wallet: str) -> bool:
    """True only if connected AND not Invisible."""
    cur = PRESENCE.get(_norm(wallet))
    return bool(cur) and cur.get("mode") != "invisible"


def presence_public_snapshot() -> dict:
    """Wallets others should see as online."""
    return {w: True for w, p in PRESENCE.items() if p.get("mode") != "invisible"}


def _role_sort_key(member: dict) -> tuple:
    """Owners/mods first, then helpers, then members; name A→Z within band."""
    role = (member.get("role") or "member").lower()
    name = (member.get("display_name") or member.get("handle") or member.get("wallet") or "").lower()
    return (-ROLE_RANK.get(role, 0), name)

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

    try:
        conn.execute('ALTER TABLE community_profiles ADD COLUMN hide_wallet INTEGER NOT NULL DEFAULT 0')
        conn.commit()
    except Exception:
        pass

    try:
        conn.execute(
            "ALTER TABLE community_profiles ADD COLUMN presence_mode TEXT NOT NULL DEFAULT 'online'"
        )
        conn.commit()
    except Exception:
        pass

    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_timeouts (
            community_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            until_ts INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            by_wallet TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (community_id, wallet)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_bans (
            community_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            by_wallet TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (community_id, wallet)
        )"""
    )
    # Shared community garden (Grow-a-Tree style bot) + external bot tokens
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_garden (
            community_id TEXT PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            waters INTEGER NOT NULL DEFAULT 0,
            stage INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            last_milestone INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_garden_waters (
            community_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            last_water_at INTEGER NOT NULL DEFAULT 0,
            total_waters INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (community_id, wallet)
        )"""
    )
    # Append-only garden audit log — survives counter resets; used to reconcile height
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_garden_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            wallet TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_garden_events_cid
           ON community_garden_events(community_id, id)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_bot_tokens (
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            community_id TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.commit()
    try:
        conn.execute(
            "ALTER TABLE community_messages ADD COLUMN edited_at INTEGER"
        )
        conn.commit()
    except Exception:
        pass

    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_stickers (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'sticker',
            name TEXT NOT NULL DEFAULT '',
            image_data TEXT NOT NULL,
            image_type TEXT NOT NULL DEFAULT 'image/png',
            uploader TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_tickets (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            opener_wallet TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            closed_at INTEGER,
            closed_by TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_ticket_messages (
            id TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            sender_wallet TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    conn.commit()


    # ensure media post-only channel exists
    try:
        row_m = conn.execute(
            "SELECT id FROM community_channels WHERE community_id=? AND slug='media'",
            (COMMUNITY_ID,),
        ).fetchone()
        if not row_m:
            conn.execute(
                """INSERT INTO community_channels
                   (id, community_id, slug, name, kind, description, sort_order, readonly_members)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f"{COMMUNITY_ID}:media",
                    COMMUNITY_ID,
                    "media",
                    "Media",
                    "media",
                    "Share images and clips — posts only, not a chat room",
                    2,
                    1,
                ),
            )
            # shift general sort if needed — keep as-is
            conn.commit()
    except Exception as e:
        print("  [community] media channel seed:", e)

    # #media: open for members to share images / GIF / video links (not staff-only)
    try:
        conn.execute(
            """UPDATE community_channels
               SET name='Media', description=?, readonly_members=0, kind='media'
               WHERE community_id=? AND slug='media'""",
            (
                "Share images, GIFs, and video links — upload or paste",
                COMMUNITY_ID,
            ),
        )
        conn.commit()
    except Exception as e:
        print("  [community] media channel open:", e)

    # #garden — shared grow-a-tree bot channel
    try:
        row_g = conn.execute(
            "SELECT id FROM community_channels WHERE community_id=? AND slug='garden'",
            (COMMUNITY_ID,),
        ).fetchone()
        if not row_g:
            conn.execute(
                """INSERT INTO community_channels
                   (id, community_id, slug, name, kind, description, sort_order, readonly_members)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f"{COMMUNITY_ID}:garden",
                    COMMUNITY_ID,
                    "garden",
                    "Tree",
                    "garden",
                    "Grow the community tree together — water it in-channel",
                    3,
                    0,
                ),
            )
        else:
            conn.execute(
                """UPDATE community_channels
                   SET name='Tree', description=?, kind='garden', readonly_members=0
                   WHERE community_id=? AND slug='garden'""",
                ("Grow the community tree together — water it in-channel", COMMUNITY_ID),
            )
        now_g = int(time.time())
        conn.execute(
            """INSERT OR IGNORE INTO community_garden
               (community_id, xp, waters, stage, updated_at, last_milestone)
               VALUES (?,?,?,?,?,?)""",
            (COMMUNITY_ID, 0, 0, 0, now_g, 0),
        )
        conn.commit()
    except Exception as e:
        print("  [community] garden channel seed:", e)

    # keep start-here / links read-only for members (migrate existing DBs)
    try:
        conn.execute(
            "UPDATE community_channels SET readonly_members=1 WHERE community_id=? AND slug IN ('start-here','links','announcements')",
            (COMMUNITY_ID,),
        )
        conn.commit()
    except Exception:
        pass

    # Ensure #Devs + #Nodes exist as open P2P chat channels
    try:
        conn.execute(
            "UPDATE community_channels SET name=?, description=?, readonly_members=0, sort_order=? WHERE community_id=? AND slug='dev'",
            (
                "Devs",
                "Builders, apps, contracts, tooling — P2P live chat",
                5,
                COMMUNITY_ID,
            ),
        )
        row_n = conn.execute(
            "SELECT id FROM community_channels WHERE community_id=? AND slug='nodes'",
            (COMMUNITY_ID,),
        ).fetchone()
        if not row_n:
            conn.execute(
                """INSERT INTO community_channels
                   (id, community_id, slug, name, kind, description, sort_order, readonly_members)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f"{COMMUNITY_ID}:nodes",
                    COMMUNITY_ID,
                    "nodes",
                    "Nodes",
                    "text",
                    "Node operators, validators, RPC, sync, hardware — P2P live chat",
                    6,
                    0,
                ),
            )
        else:
            conn.execute(
                "UPDATE community_channels SET name=?, description=?, readonly_members=0 WHERE community_id=? AND slug='nodes'",
                (
                    "Nodes",
                    "Node operators, validators, RPC, sync, hardware — P2P live chat",
                    COMMUNITY_ID,
                ),
            )
        conn.commit()
    except Exception as e:
        print("  [community] devs/nodes channel seed:", e)

    # Ensure #Mods staff-only P2P channel
    try:
        row_m = conn.execute(
            "SELECT id FROM community_channels WHERE community_id=? AND slug='mods'",
            (COMMUNITY_ID,),
        ).fetchone()
        if not row_m:
            conn.execute(
                """INSERT INTO community_channels
                   (id, community_id, slug, name, kind, description, sort_order, readonly_members)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    f"{COMMUNITY_ID}:mods",
                    COMMUNITY_ID,
                    "mods",
                    "Mods",
                    "text",
                    "Staff only — mod issues & coordination · P2P",
                    9,
                    0,
                ),
            )
        else:
            conn.execute(
                "UPDATE community_channels SET name=?, description=?, readonly_members=0 WHERE community_id=? AND slug='mods'",
                ("Mods", "Staff only — mod issues & coordination · P2P", COMMUNITY_ID),
            )
        conn.commit()
    except Exception as e:
        print("  [community] mods channel seed:", e)

    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_threads (
            id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            channel_slug TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_thread_members (
            thread_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            added_by TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (thread_id, wallet)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS community_thread_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            sender_wallet TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )
    conn.commit()

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
    """Join as member if not in community. First human join becomes owner if no owners.
    Returns None if wallet invalid or banned (banned users cannot rejoin)."""
    wallet = _norm(wallet)
    if not wallet.startswith("0x"):
        return None
    if is_banned(conn, wallet):
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


def is_staff(role: str) -> bool:
    """Admins (owner/admin) and mods may post links."""
    return (role or "") in ("owner", "admin", "mod")


_LINK_RE = re.compile(
    r"(https?://|www\.|"
    r"(?:^|[\s(])(?:discord\.gg|t\.me|telegram\.me)/|"
    r"\b[a-z0-9-]+\.(?:com|org|net|io|ai|xyz|app|gg|me|co|info|dev)(?:/[^\s]*)?)",
    re.I,
)


def content_has_link(content: str) -> bool:
    """True if message contains a URL / invite-style link (incl. [[gif]]/[[img]]/[[video]]).
    Built-in/custom stickers use [[sticker]]… and are NOT treated as links."""
    c = (content or "").strip()
    if not c:
        return False
    if c.startswith("[[sticker]]") or c.startswith("[[emoji]]"):
        return False
    if c.startswith("[[gif]]") or c.startswith("[[img]]") or c.startswith("[[video]]"):
        return True
    return bool(_LINK_RE.search(c))


_MEDIA_HOST_OK = (
    "tenor.com", "tenor.co", "giphy.com", "giphy.gif",
    "imgur.com", "i.imgur.com", "media.discordapp.net", "cdn.discordapp.com",
)


def is_media_only_content(content: str) -> bool:
    """True if message is an allowed media post (image embed, gif, or video URL)."""
    c = (content or "").strip()
    if not c:
        return False
    if c.startswith("[[sticker]]") or c.startswith("[[emoji]]"):
        return True
    if c.startswith("[[img]]"):
        url = c[7:].strip()
        return (
            url.startswith("https://")
            or url.startswith("/chat-image/")
            or "/chat-image/" in url
            or url.startswith("/chat-file/")
            or "/chat-file/" in url
        )
    if c.startswith("[[gif]]"):
        url = c[7:].strip().lower()
        return url.startswith("https://") and any(h in url for h in ("tenor.com", "tenor.co", "giphy.com"))
    if c.startswith("[[video]]"):
        url = c[9:].strip()
        low = url.lower()
        if "/chat-file/" in low or low.startswith("/chat-file/"):
            return True
        if not low.startswith("https://"):
            return False
        if any(x in low for x in (".mp4", ".webm", ".mov", "youtube.com", "youtu.be", "vimeo.com")):
            return True
        return any(h in low for h in _MEDIA_HOST_OK)
    # bare https image/video URL
    low = c.lower()
    if low.startswith("https://") and " " not in c.strip():
        if any(low.endswith(ext) or ext + "?" in low for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm")):
            return True
        if any(h in low for h in ("youtube.com", "youtu.be", "vimeo.com")):
            return True
    return False


_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{2,40}|everyone)\b", re.I)


def parse_mentions(conn, content: str) -> tuple[bool, list[str]]:
    """Return (everyone, [wallets]). Resolves @handle / @display_name (no leading @ in DB)."""
    everyone = False
    wallets: set[str] = set()
    if not content:
        return False, []
    for m in _MENTION_RE.finditer(content):
        token = (m.group(1) or "").lower()
        if token == "everyone":
            everyone = True
            continue
        # handle match (with or without legacy @)
        row = conn.execute(
            "SELECT wallet FROM handles WHERE handle = ? OR handle = ?",
            (token, "@" + token),
        ).fetchone()
        if row:
            wallets.add(_norm(row["wallet"]))
            continue
        # display_name match (case-insensitive)
        row = conn.execute(
            """SELECT wallet FROM community_profiles
               WHERE lower(display_name) = ? OR lower(display_name) = ? LIMIT 1""",
            (token, "@" + token),
        ).fetchone()
        if row:
            wallets.add(_norm(row["wallet"]))
            continue
        # raw 0x wallet mention @0xabc… (first 6+ chars unique enough — skip unless full)
        if token.startswith("0x") and len(token) == 42:
            wallets.add(_norm(token))
    return everyone, list(wallets)


def all_member_wallets(conn) -> list[str]:
    rows = conn.execute(
        "SELECT wallet FROM community_members WHERE community_id=?",
        (COMMUNITY_ID,),
    ).fetchall()
    return [_norm(r["wallet"]) for r in rows]


def emit_community_mentions(socketio, conn, msg: dict, everyone: bool, wallets: list, sender: str):
    """Alert mentioned members (wallet rooms). @everyone → all members except sender."""
    payload = {
        "message_id": msg.get("id"),
        "slug": msg.get("slug"),
        "from_wallet": msg.get("sender_wallet"),
        "from_name": msg.get("display_name"),
        "preview": (msg.get("content") or "")[:140],
        "everyone": bool(everyone),
    }
    targets: set[str] = set()
    if everyone:
        targets.update(all_member_wallets(conn))
    for w in wallets or []:
        if w:
            targets.add(_norm(w))
    targets.discard(_norm(sender))
    for w in targets:
        try:
            socketio.emit("community_mention", payload, room=w)
        except Exception:
            pass


def get_timeout_until(conn, wallet: str) -> int:
    wallet = _norm(wallet)
    row = conn.execute(
        "SELECT until_ts FROM community_timeouts WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, wallet),
    ).fetchone()
    if not row:
        return 0
    until = int(row["until_ts"] or 0)
    now = int(time.time())
    if until <= now:
        conn.execute(
            "DELETE FROM community_timeouts WHERE community_id=? AND wallet=?",
            (COMMUNITY_ID, wallet),
        )
        conn.commit()
        return 0
    return until


def set_timeout(conn, wallet: str, seconds: int, reason: str, by_wallet: str = "") -> int:
    wallet = _norm(wallet)
    now = int(time.time())
    until = now + max(60, int(seconds))
    conn.execute(
        """INSERT INTO community_timeouts
           (community_id, wallet, until_ts, reason, by_wallet, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(community_id, wallet) DO UPDATE SET
             until_ts=excluded.until_ts,
             reason=excluded.reason,
             by_wallet=excluded.by_wallet,
             created_at=excluded.created_at""",
        (COMMUNITY_ID, wallet, until, (reason or "")[:200], _norm(by_wallet), now),
    )
    conn.commit()
    return until


def clear_timeout(conn, wallet: str) -> None:
    conn.execute(
        "DELETE FROM community_timeouts WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, _norm(wallet)),
    )
    conn.commit()


def is_banned(conn, wallet: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM community_bans WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, _norm(wallet)),
    ).fetchone()
    return bool(row)


def set_ban(conn, wallet: str, reason: str, by_wallet: str = "") -> None:
    wallet = _norm(wallet)
    now = int(time.time())
    conn.execute(
        """INSERT INTO community_bans (community_id, wallet, reason, by_wallet, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(community_id, wallet) DO UPDATE SET
             reason=excluded.reason,
             by_wallet=excluded.by_wallet,
             created_at=excluded.created_at""",
        (COMMUNITY_ID, wallet, (reason or "")[:200], _norm(by_wallet), now),
    )
    # Also remove membership so they are out of the server
    conn.execute(
        "DELETE FROM community_members WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, wallet),
    )
    conn.commit()


def clear_ban(conn, wallet: str) -> None:
    conn.execute(
        "DELETE FROM community_bans WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, _norm(wallet)),
    )
    conn.commit()


def kick_member(conn, wallet: str) -> None:
    """Remove from community; they may rejoin later (unlike ban)."""
    wallet = _norm(wallet)
    conn.execute(
        "DELETE FROM community_members WHERE community_id=? AND wallet=?",
        (COMMUNITY_ID, wallet),
    )
    clear_timeout(conn, wallet)
    conn.commit()


TIMEOUT_DURATIONS = {
    "1h": 3600,
    "24h": 86400,
    "1w": 604800,
    "1hr": 3600,
    "24hr": 86400,
    "1wk": 604800,
}


def can_see_ticket(conn, ticket: dict, wallet: str) -> bool:
    """Opener + all mods/admins can see a ticket; other members cannot."""
    wallet = _norm(wallet)
    if not ticket:
        return False
    if _norm(ticket.get("opener_wallet") or "") == wallet:
        return True
    role = get_role(conn, wallet)
    return is_staff(role)


def can_see_thread(conn, thread_id: str, wallet: str) -> bool:
    """Tagged members + creator; owners/admins can see all threads."""
    wallet = _norm(wallet)
    role = get_role(conn, wallet)
    if role in ("owner", "admin"):
        return True
    row = conn.execute(
        "SELECT 1 FROM community_thread_members WHERE thread_id=? AND wallet=?",
        (thread_id, wallet),
    ).fetchone()
    return bool(row)


def can_moderate(actor_role: str, target_role: str, action: str = "") -> bool:
    """Who can timeout/kick/ban/untimeout/unban whom.
    - Admin/owner: anyone (mod or not), except cannot target another owner unless actor is owner
    - Mod: other mods + non-mods; cannot target admin/owner
    """
    a = (actor_role or "").lower()
    t = (target_role or "member").lower()
    if a not in ("owner", "admin", "mod"):
        return False
    if a in ("owner", "admin"):
        # Admins can act on mods and members; owners can act on anyone except
        # we still block targeting a higher/equal owner from admin
        if t == "owner" and a != "owner":
            return False
        return True
    # Mod: may act on mods, helpers, members — not admins/owners
    if t in ("owner", "admin"):
        return False
    return True


# Grow-a-Tree (Discord-style): collaborative water + fruit catch
# Mechanics inspired by popular Discord "Grow a Tree" bots (not affiliated).
# Rules copied from Grow a Tree: no same waterer twice in a row; cooldown
# scales with height; fruit drops into basket lanes you click to catch.
GARDEN_STAGES = [
    # min_size (waters/ft), emoji, label, art lines — denser early so it visibly grows often
    (0, "🌱", "Seed", ["☁️  ☀️  ☁️", "", "   🌱", "  ═══", "▓▓▓▓▓▓▓"]),
    (2, "🌱", "Sprouting", ["☁️  ☀️  ☁️", "", "   🌱", "   │", "  ═══", "▓▓▓▓▓▓▓"]),
    (4, "🌿", "Sprout", ["☁️     ☁️", "   🌿", "   │", "  ═══", "▓▓▓▓▓▓▓"]),
    (6, "🌿", "Tall Sprout", ["☁️  ☀️  ☁️", "   🌿", "   │", "   │", "  ═══", "▓▓▓▓▓▓▓"]),
    (8, "🪴", "Baby Sapling", ["☁️     ☁️", "   🪴", "  ╱│╲", "   │", "  ═══", "▓▓▓▓▓▓▓"]),
    (12, "🪴", "Sapling", ["☁️  ☀️  ☁️", "   🪴", "  ╱│╲", " ╱ │ ╲", "══╪══", "▓▓▓▓▓▓▓"]),
    (18, "🪴", "Young Sapling", ["☁️     ☁️", "  🪴🪴", " ╱│╲│╲", "  │ │", "═╪═╪═", "▓▓▓▓▓▓▓▓▓"]),
    (25, "🌳", "Little Tree", ["☁️  ☀️  ☁️", "   🌳", "  ╱│╲", " ╱ │ ╲", "══╪══", "▓▓▓▓▓▓▓"]),
    (35, "🌳", "Young Tree", ["☁️     ☁️", "  🌳🌳", " ╱│╲╱│╲", "  │  │", "══╪══╪══", "▓▓▓▓▓▓▓▓▓"]),
    (50, "🌳", "Tree", ["☁️  ☀️  ☁️", " 🌳🌳🌳", "╱│╲│╱│╲", " │ │ │", "═╪═╪═╪═", "▓▓▓▓▓▓▓▓▓▓▓"]),
    (75, "🌲", "Tall Tree", ["☁️     ☁️", " 🌲🌲🌲", "╱│╲│╱│╲", " │ │ │", " │ │ │", "═╪═╪═╪═", "▓▓▓▓▓▓▓▓▓▓▓"]),
    (100, "🌲", "Forest Tree", ["☁️  ☀️  ☁️", "🌲🌳🌲🌳", "╱│╲│╱│╲│", " │ │ │ │", "═╪═╪═╪═╪═", "▓▓▓▓▓▓▓▓▓▓▓▓▓"]),
    (150, "🌴", "Small Grove", ["✨  ☀️  ✨", "🌴🌳🌲", "╱│╲│╱│╲", "═╬═╬═╬═", "▓▓▓▓▓▓▓▓▓▓▓"]),
    (250, "🌴", "Grove", ["✨  ☀️  ✨", "🌴🌳🌲🌳🌴", "╱│╲│╱│╲│╱", "═╬═╬═╬═╬═", "▓▓▓▓▓▓▓▓▓▓▓▓▓"]),
    (400, "🌴", "Grand Grove", ["✨🌙✨", "🌴🌳🌲🌳🌴🌳", "╱│╲│╱│╲│╱│╲", "═╩═╩═╩═╩═╩═", "▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓"]),
    (500, "🏯", "Legendary Grove", ["✨🌙✨", "🏯🌳🏯", "🌴🌲🌳🌲🌴", "═╩═╩═╩═╩═", "▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓"]),
]
GARDEN_BOT_NAME = "Garden Bot"
GARDEN_APPLE_LANES = 3
GARDEN_APPLE_CATCH_SEC = 10
# Live apple drops (drop_id -> {lane, exp, caught, by})
GARDEN_APPLE_DROPS: dict[str, dict] = {}


def garden_stage_for_size(size: int) -> tuple[int, str, str, list]:
    stage_i = 0
    emoji, label, art = GARDEN_STAGES[0][1], GARDEN_STAGES[0][2], GARDEN_STAGES[0][3]
    for i, (need, em, lab, ar) in enumerate(GARDEN_STAGES):
        if size >= need:
            stage_i, emoji, label, art = i, em, lab, ar
    return stage_i, emoji, label, art


def garden_stage_for_xp(xp: int) -> tuple[int, str, str, list]:
    """Back-compat alias — stages track water count (tree size)."""
    return garden_stage_for_size(xp)


def garden_next_need(size: int) -> int | None:
    for need, *_rest in GARDEN_STAGES:
        if size < need:
            return need
    return None


def garden_cooldown_for_size(size: int) -> int:
    """Discord Grow a Tree formula: floor((size * 0.07 + 5) ** 1.1) seconds."""
    size = max(1, int(size or 1))
    return max(5, int((size * 0.07 + 5) ** 1.1))


def garden_cooldown_for_stage(stage_i: int) -> int:
    """Rough stage-based fallback (prefer garden_cooldown_for_size)."""
    approx = [need for need, *_ in GARDEN_STAGES]
    i = max(0, min(int(stage_i or 0), len(approx) - 1))
    return garden_cooldown_for_size(approx[i] or 1)


def garden_height_ft(size: int, last_watered_at: int = 0, cooldown_sec: int = 0) -> float:
    """Discord-style height: whole feet when ready; fractional while growing."""
    size = max(0, int(size or 0))
    if size <= 0:
        return 0.0
    now = int(time.time())
    if cooldown_sec > 0 and last_watered_at > 0:
        ready_at = last_watered_at + cooldown_sec
        if ready_at > now:
            # Count down the last 1.0 ft while growing
            frac = (ready_at - now) / float(cooldown_sec)
            return max(0.1, round(size - frac, 1))
    return float(size)


def garden_ensure_columns(conn) -> None:
    for sql in (
        "ALTER TABLE community_garden ADD COLUMN last_waterer TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE community_garden ADD COLUMN apples INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE community_garden_waters ADD COLUMN apples INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS community_garden_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            wallet TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_garden_events_cid
           ON community_garden_events(community_id, id)""",
    ):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass


def garden_log_event(conn, kind: str, wallet: str = "", size: int = 0, detail: str = "") -> None:
    """Append-only water/catch/restore event (persistence / rebuild source)."""
    garden_ensure_columns(conn)
    try:
        conn.execute(
            """INSERT INTO community_garden_events
               (community_id, kind, wallet, size, detail, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                COMMUNITY_ID,
                (kind or "event")[:40],
                _norm(wallet or ""),
                int(size or 0),
                (detail or "")[:200],
                int(time.time()),
            ),
        )
    except Exception as e:
        print("  [garden] event log failed:", e)


def garden_reconcile_from_events(conn) -> bool:
    """If counters were wiped but events remain, restore waters/xp/stage from max size."""
    garden_ensure_columns(conn)
    try:
        row = conn.execute(
            """SELECT MAX(size) AS mx FROM community_garden_events
               WHERE community_id=? AND kind IN ('water','restore')""",
            (COMMUNITY_ID,),
        ).fetchone()
        mx = int(row["mx"] or 0) if row and row["mx"] is not None else 0
    except Exception:
        return False
    if mx <= 0:
        return False
    g = conn.execute(
        "SELECT waters, xp FROM community_garden WHERE community_id=?",
        (COMMUNITY_ID,),
    ).fetchone()
    cur = int(g["waters"] or 0) if g else 0
    if cur >= mx:
        return False
    stage_i, *_rest = garden_stage_for_size(mx)
    now = int(time.time())
    old_xp = int(g["xp"] or 0) if g else 0
    new_xp = max(old_xp, mx)
    if g:
        conn.execute(
            """UPDATE community_garden
               SET waters=?, xp=?, stage=?, updated_at=?
               WHERE community_id=?""",
            (mx, new_xp, stage_i, now, COMMUNITY_ID),
        )
    else:
        conn.execute(
            """INSERT INTO community_garden
               (community_id, xp, waters, stage, updated_at, last_milestone)
               VALUES (?,?,?,?,?,?)""",
            (COMMUNITY_ID, new_xp, mx, stage_i, now, 0),
        )
    conn.commit()
    print(f"  [garden] reconciled waters {cur} → {mx} from event log")
    return True


def garden_prune_apple_drops() -> None:
    now = time.time()
    dead = [
        did
        for did, d in GARDEN_APPLE_DROPS.items()
        if d.get("caught") or float(d.get("exp") or 0) < now
    ]
    for did in dead:
        GARDEN_APPLE_DROPS.pop(did, None)


def garden_spawn_apple(socketio=None) -> dict | None:
    """Spawn a fruit drop across 3 basket lanes (Discord fruit-harvest style)."""
    garden_prune_apple_drops()
    # One live drop at a time
    if any(not d.get("caught") and float(d.get("exp") or 0) >= time.time() for d in GARDEN_APPLE_DROPS.values()):
        return None
    drop_id = str(uuid.uuid4())
    lane = random.randint(0, GARDEN_APPLE_LANES - 1)
    exp = time.time() + GARDEN_APPLE_CATCH_SEC
    drop = {
        "id": drop_id,
        "lane": lane,
        "lanes": GARDEN_APPLE_LANES,
        "exp": exp,
        "expires_in": GARDEN_APPLE_CATCH_SEC,
        "caught": False,
        "by": "",
        "by_name": "",
    }
    GARDEN_APPLE_DROPS[drop_id] = drop
    if socketio is not None:
        try:
            socketio.emit("community_garden_apple", drop, room="community:garden")
        except Exception:
            pass
    return drop


def garden_active_apple() -> dict | None:
    garden_prune_apple_drops()
    for d in GARDEN_APPLE_DROPS.values():
        if not d.get("caught") and float(d.get("exp") or 0) >= time.time():
            left = max(0, int(float(d["exp"]) - time.time()))
            return {**d, "expires_in": left}
    return None


def garden_contributors(conn, limit: int = 10) -> list[dict]:
    """Top waterers — Discord Grow-a-Tree style contributor ranking."""
    garden_ensure_columns(conn)
    rows = conn.execute(
        """SELECT wallet, total_waters, COALESCE(apples, 0) AS apples
           FROM community_garden_waters
           WHERE community_id=? AND total_waters > 0
           ORDER BY total_waters DESC, apples DESC
           LIMIT ?""",
        (COMMUNITY_ID, max(1, min(int(limit or 10), 25))),
    ).fetchall()
    out = []
    for i, r in enumerate(rows):
        w = _norm(r["wallet"] or "")
        name = ""
        if w.startswith("0x"):
            try:
                name = profile_dict(conn, w).get("display_name") or (
                    w[:6] + "…" + w[-4:] if len(w) > 10 else w
                )
            except Exception:
                name = (w[:6] + "…" + w[-4:]) if len(w) > 10 else w
        out.append({
            "rank": i + 1,
            "wallet": w,
            "name": name or "Gardener",
            "waters": int(r["total_waters"] or 0),
            "apples": int(r["apples"] or 0),
        })
    return out


def garden_state_dict(conn, viewer: str = "") -> dict:
    garden_ensure_columns(conn)
    try:
        garden_reconcile_from_events(conn)
    except Exception:
        pass
    row = conn.execute(
        "SELECT * FROM community_garden WHERE community_id=?",
        (COMMUNITY_ID,),
    ).fetchone()
    if not row:
        return {
            "xp": 0,
            "waters": 0,
            "stage": 0,
            "emoji": "🌱",
            "label": "Seed",
            "art": GARDEN_STAGES[0][3],
            "height_ft": 0,
            "next_xp": 5,
            "updated_at": 0,
            "cooldown_sec": garden_cooldown_for_size(1),
            "cooldown_remaining": 0,
            "can_water": bool(viewer and viewer.startswith("0x")),
            "blocked_reason": "",
            "last_waterer": "",
            "last_waterer_name": "",
            "apples": 0,
            "my_apples": 0,
            "my_waters": 0,
            "my_rank": None,
            "contributors": [],
            "apple_drop": None,
            "tree_name": "Lightchain Tree",
        }
    size = int(row["waters"] or 0)
    xp = int(row["xp"] or 0)
    # Prefer waters as tree size; fall back to xp for older rows
    if size <= 0 and xp > 0:
        size = xp
    stage_i, emoji, label, art = garden_stage_for_size(size)
    last_w = ""
    try:
        last_w = _norm(row["last_waterer"] or "")
    except Exception:
        last_w = ""
    last_name = ""
    if last_w.startswith("0x"):
        try:
            last_name = profile_dict(conn, last_w).get("display_name") or (
                last_w[:6] + "…" + last_w[-4:]
            )
        except Exception:
            last_name = last_w[:8] + "…"
    apples_total = 0
    try:
        apples_total = int(row["apples"] or 0)
    except Exception:
        apples_total = 0
    my_apples = 0
    my_waters = 0
    viewer_n = _norm(viewer) if viewer else ""
    if viewer_n.startswith("0x"):
        wr = conn.execute(
            """SELECT apples, total_waters FROM community_garden_waters
               WHERE community_id=? AND wallet=?""",
            (COMMUNITY_ID, viewer_n),
        ).fetchone()
        if wr:
            try:
                my_apples = int(wr["apples"] or 0)
            except Exception:
                my_apples = 0
            try:
                my_waters = int(wr["total_waters"] or 0)
            except Exception:
                my_waters = 0
    last_at = int(row["updated_at"] or 0)
    cd = garden_cooldown_for_size(max(1, size))
    now = int(time.time())
    left = max(0, (last_at + cd) - now) if last_at else 0
    # Discord rules: ready when cooldown done AND you weren't last waterer
    blocked = ""
    can = False
    if viewer_n.startswith("0x"):
        if last_w and viewer_n == last_w and size > 0:
            blocked = "same_waterer"
            can = False
        elif left > 0:
            blocked = "growing"
            can = False
        else:
            can = True
    height = garden_height_ft(size, last_at, cd)
    contributors = garden_contributors(conn, limit=10)
    my_rank = None
    if viewer_n.startswith("0x"):
        for c in contributors:
            if (c.get("wallet") or "").lower() == viewer_n.lower():
                my_rank = c.get("rank")
                break
        if my_rank is None and my_waters > 0:
            # Outside top 10 — compute exact rank
            ahead = conn.execute(
                """SELECT COUNT(*) AS n FROM community_garden_waters
                   WHERE community_id=? AND total_waters > ?""",
                (COMMUNITY_ID, my_waters),
            ).fetchone()
            my_rank = int(ahead["n"] or 0) + 1 if ahead else None
    return {
        "xp": xp if xp else size,
        "waters": size,
        "stage": stage_i,
        "emoji": emoji,
        "label": label,
        "art": art,
        "height_ft": height,
        "next_xp": garden_next_need(size),
        "updated_at": last_at,
        "cooldown_sec": cd,
        "cooldown_remaining": left,
        "can_water": can,
        "blocked_reason": blocked,
        "last_waterer": last_w,
        "last_waterer_name": last_name,
        "apples": apples_total,
        "my_apples": my_apples,
        "my_waters": my_waters,
        "my_rank": my_rank,
        "contributors": contributors,
        "apple_drop": garden_active_apple(),
        "tree_name": "Lightchain Tree",
    }


def post_channel_bot_message(conn, socketio, slug: str, content: str, bot_name: str = "Bot") -> dict | None:
    """Insert a channel message as a bot (no real wallet) and emit live."""
    ch = conn.execute(
        "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
        (COMMUNITY_ID, slug),
    ).fetchone()
    if not ch:
        return None
    mid = str(uuid.uuid4())
    now = int(time.time())
    # Synthetic sender — not a real 0x wallet; clients show bot_name
    sender = "bot:garden" if "garden" in (bot_name or "").lower() else "bot:system"
    conn.execute(
        """INSERT INTO community_messages
           (id, community_id, channel_id, sender_wallet, content, created_at)
           VALUES (?,?,?,?,?,?)""",
        (mid, COMMUNITY_ID, ch["id"], sender, content, now),
    )
    conn.commit()
    msg = {
        "id": mid,
        "sender_wallet": sender,
        "content": content,
        "created_at": now,
        "display_name": bot_name,
        "role": "bot",
        "has_avatar": False,
        "slug": slug,
        "bot": True,
    }
    try:
        socketio.emit("community_message", msg, room=f"community:{slug}")
    except Exception:
        pass
    return msg


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
    handle = (h["handle"] if h else "") or ""
    handle = handle.lstrip("@")
    display = (display or "").lstrip("@")
    if not display:
        display = handle or (wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet)
    hide = False
    presence_mode = "online"
    try:
        row_h = conn.execute(
            "SELECT hide_wallet, presence_mode FROM community_profiles WHERE wallet=?",
            (wallet,),
        ).fetchone()
        if row_h:
            if row_h["hide_wallet"]:
                hide = True
            pm = row_h["presence_mode"] if "presence_mode" in row_h.keys() else None
            if pm in ("online", "invisible"):
                presence_mode = pm
    except Exception:
        try:
            row_h = conn.execute(
                "SELECT hide_wallet FROM community_profiles WHERE wallet=?", (wallet,)
            ).fetchone()
            if row_h and row_h["hide_wallet"]:
                hide = True
        except Exception:
            pass
    live = PRESENCE.get(wallet)
    connected = bool(live)
    # Public online = connected and not Invisible. Self still gets presence_mode.
    online = connected and presence_mode != "invisible" and (
        not live or live.get("mode") != "invisible"
    )
    # Prefer live mode if connected (may have changed before DB save)
    if live and live.get("mode") in ("online", "invisible"):
        presence_mode = live["mode"]
        online = connected and presence_mode != "invisible"
    return {
        "wallet": wallet,
        "display_name": display,
        "bio": bio,
        "handle": handle,
        "role": role or "member",
        "has_avatar": has_avatar,
        "avatar_url": f"/api/community/avatar/{wallet}" if has_avatar else None,
        "hide_wallet": hide,
        "presence_mode": presence_mode,
        "online": online,
        "connected": connected,
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
            # Modes from DB flag: flip readonly_members 0↔1 to open chat as we grow
            GUIDE_SLUGS = {"start-here"}
            STAFF_ONLY_SLUGS = {"mods"}
            PRIMARY_SLUGS = {
                "general", "dev", "nodes", "mods",
                "start-here", "announcements", "media", "links", "garden",
            }
            P2P_CHAT_SLUGS = {"general", "dev", "nodes", "mods"}
            # wallet from query for staff-only channel filtering
            viewer = _norm(request.args.get("wallet", ""))
            viewer_role = get_role(conn, viewer) if viewer else None
            enriched = []
            for c in channels:
                d = dict(c)
                slug = d["slug"]
                ro = int(d.get("readonly_members") or 0)
                if slug in STAFF_ONLY_SLUGS:
                    if not is_staff(viewer_role):
                        continue  # hide #mods from non-staff
                    d["mode"] = "chat"
                    d["staff_only"] = True
                elif slug in GUIDE_SLUGS:
                    d["mode"] = "guide"
                elif slug == "media":
                    d["mode"] = "media"  # everyone can post images/GIF/video links
                    d["staff_only"] = False
                elif slug == "garden":
                    d["mode"] = "garden"  # shared grow-a-tree + chat
                    d["staff_only"] = False
                elif slug in P2P_CHAT_SLUGS or ro == 0:
                    d["mode"] = "chat"
                    d["staff_only"] = False
                else:
                    d["mode"] = "post"
                    d["staff_only"] = False
                d["primary"] = slug in PRIMARY_SLUGS or d["mode"] in ("chat", "media", "garden")
                if slug not in PRIMARY_SLUGS and d["mode"] != "chat":
                    d["mode"] = "hidden"
                    d["primary"] = False
                if d["mode"] == "hidden":
                    continue
                enriched.append(d)
            return jsonify({
                "id": COMMUNITY_ID,
                "name": meta["name"] if meta else "Lightchain",
                "description": meta["description"] if meta else "",
                "member_count": n_members,
                "channels": enriched,
                "chat_channel": "general",
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
            if is_banned(conn, wallet):
                return jsonify({"error": "You are banned from this community", "code": "banned"}), 403
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "Could not join"}), 400
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
            if is_banned(conn, wallet):
                return jsonify({
                    "error": "banned",
                    "code": "banned",
                    "role": None,
                    "banned": True,
                }), 403
            role = ensure_member(conn, wallet)
            return jsonify({"role": role, "profile": profile_dict(conn, wallet), "perms": list(ROLE_PERMS.get(role, []))})
        finally:
            conn.close()

    @app.route("/api/community/moderate", methods=["POST"])
    def api_moderate():
        """Moderation: timeout | kick | ban | unban | untimeout.
        Admin/owner: anyone (incl. mods). Mod: other mods + non-mods (not admins).
        Body: wallet (actor), target, action, duration? (1h|24h|1w), reason?
        """
        data = request.json or {}
        actor = _norm(data.get("wallet", ""))
        target = _norm(data.get("target", ""))
        action = (data.get("action") or "").lower().strip()
        duration = (data.get("duration") or "1h").lower().strip()
        reason = (data.get("reason") or "")[:200]
        if not actor.startswith("0x") or not target.startswith("0x"):
            return jsonify({"error": "wallet and target required"}), 400
        if actor == target:
            return jsonify({"error": "cannot moderate yourself"}), 400
        conn = get_db()
        try:
            a_role = ensure_member(conn, actor)
            if not is_staff(a_role):
                return jsonify({"error": "mods and admins only"}), 403
            t_role = get_role(conn, target) or "member"
            # If banned, they have no membership — treat as member for permission checks
            if is_banned(conn, target) and action in ("unban", "untimeout"):
                t_role = "member"
            if not can_moderate(a_role, t_role, action):
                return jsonify({
                    "error": "not allowed — admins can moderate anyone; mods can moderate mods and members only",
                }), 403

            now = int(time.time())
            if action == "timeout":
                secs = TIMEOUT_DURATIONS.get(duration)
                if not secs:
                    return jsonify({"error": "duration must be 1h, 24h, or 1w"}), 400
                until = set_timeout(conn, target, secs, reason or f"Timeout {duration}", actor)
                payload = {
                    "ok": True,
                    "action": "timeout",
                    "target": target,
                    "until": until,
                    "duration": duration,
                    "remaining": until - now,
                }
                try:
                    socketio.emit("community_timeout", {
                        "wallet": target,
                        "until": until,
                        "reason": reason or f"Timed out ({duration})",
                        "remaining": until - now,
                        "by": actor,
                    })
                except Exception:
                    pass
                return jsonify(payload)

            if action == "untimeout":
                clear_timeout(conn, target)
                try:
                    socketio.emit("community_timeout", {
                        "wallet": target,
                        "until": 0,
                        "reason": "Timeout cleared",
                        "remaining": 0,
                        "by": actor,
                    })
                except Exception:
                    pass
                return jsonify({"ok": True, "action": "untimeout", "target": target})

            if action == "kick":
                kick_member(conn, target)
                try:
                    socketio.emit("community_kicked", {"wallet": target, "by": actor, "reason": reason})
                except Exception:
                    pass
                return jsonify({"ok": True, "action": "kick", "target": target})

            if action == "ban":
                set_ban(conn, target, reason or "Banned", actor)
                try:
                    socketio.emit("community_banned", {"wallet": target, "by": actor, "reason": reason})
                except Exception:
                    pass
                return jsonify({"ok": True, "action": "ban", "target": target})

            if action == "unban":
                clear_ban(conn, target)
                return jsonify({"ok": True, "action": "unban", "target": target})

            return jsonify({"error": "action must be timeout|untimeout|kick|ban|unban"}), 400
        finally:
            conn.close()

    @app.route("/api/community/directory")
    def api_directory():
        q = (request.args.get("q") or "").strip().lower()
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT wallet, role, joined_at FROM community_members
                   WHERE community_id=? LIMIT 200""",
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
            # Discord-style: role rank (owners/mods first), then name
            out.sort(key=_role_sort_key)
            online = [m for m in out if m.get("online")]
            offline = [m for m in out if not m.get("online")]
            return jsonify({
                "members": out,
                "online": online,
                "offline": offline,
                "count": len(out),
                "online_count": len(online),
            })
        finally:
            conn.close()

    @app.route("/api/community/presence")
    def api_presence():
        """Public online wallets (Invisible excluded)."""
        return jsonify({"online": list(presence_public_snapshot().keys())})

    @app.route("/api/community/profile", methods=["POST"])
    def api_profile_update():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        display = (data.get("display_name") or "")[:40].strip().lstrip("@")
        bio = (data.get("bio") or "")[:280].strip()
        hide_wallet = 1 if data.get("hide_wallet") in (True, 1, "1", "true", "True") else 0
        if "hide_wallet" not in data:
            hide_wallet = None  # don't overwrite if omitted
        presence_mode = None
        if "presence_mode" in data:
            pm = (data.get("presence_mode") or "").lower().strip()
            if pm not in ("online", "invisible"):
                return jsonify({"error": "presence_mode must be online or invisible"}), 400
            presence_mode = pm
        display = display.lstrip("@")
        now = int(time.time())
        conn = get_db()
        try:
            ensure_member(conn, wallet)
            existing = conn.execute(
                "SELECT wallet FROM community_profiles WHERE wallet=?", (wallet,)
            ).fetchone()
            if existing:
                # Build update dynamically for optional fields
                sets = ["display_name=?", "bio=?", "updated_at=?"]
                vals = [display, bio, now]
                if hide_wallet is not None:
                    sets.append("hide_wallet=?")
                    vals.append(hide_wallet)
                if presence_mode is not None:
                    sets.append("presence_mode=?")
                    vals.append(presence_mode)
                vals.append(wallet)
                conn.execute(
                    f"UPDATE community_profiles SET {', '.join(sets)} WHERE wallet=?",
                    vals,
                )
            else:
                conn.execute(
                    """INSERT INTO community_profiles
                       (wallet, display_name, bio, hide_wallet, presence_mode, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        wallet,
                        display,
                        bio,
                        hide_wallet or 0,
                        presence_mode or "online",
                        now,
                    ),
                )
            conn.commit()
            if presence_mode is not None and wallet in PRESENCE:
                presence_set(wallet, presence_mode, PRESENCE[wallet].get("sid"))
                try:
                    socketio.emit(
                        "presence_update",
                        {
                            "wallet": wallet,
                            "online": presence_is_visible_online(wallet),
                            "mode": presence_mode,
                        },
                    )
                except Exception:
                    pass
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
        viewer = _norm(request.args.get("wallet", ""))
        conn = get_db()
        try:
            if slug == "mods":
                role = get_role(conn, viewer) if viewer else None
                if not is_staff(role):
                    return jsonify({"error": "Mods channel is staff only", "code": "staff_only"}), 403
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            try:
                rows = conn.execute(
                    """SELECT id, sender_wallet, content, created_at, edited_at FROM community_messages
                       WHERE channel_id=? ORDER BY created_at DESC LIMIT ?""",
                    (ch["id"], limit),
                ).fetchall()
            except Exception:
                rows = conn.execute(
                    """SELECT id, sender_wallet, content, created_at FROM community_messages
                       WHERE channel_id=? ORDER BY created_at DESC LIMIT ?""",
                    (ch["id"], limit),
                ).fetchall()
            msgs = []
            for r in reversed(list(rows)):
                sw = r["sender_wallet"] or ""
                edited_at = None
                try:
                    edited_at = r["edited_at"]
                except Exception:
                    edited_at = None
                if str(sw).startswith("bot:"):
                    msgs.append(
                        {
                            "id": r["id"],
                            "sender_wallet": sw,
                            "content": r["content"],
                            "created_at": r["created_at"],
                            "edited": bool(edited_at),
                            "edited_at": edited_at,
                            "display_name": GARDEN_BOT_NAME if "garden" in sw else "Bot",
                            "role": "bot",
                            "has_avatar": False,
                            "bot": True,
                        }
                    )
                    continue
                prof = profile_dict(conn, sw)
                msgs.append(
                    {
                        "id": r["id"],
                        "sender_wallet": sw,
                        "content": r["content"],
                        "created_at": r["created_at"],
                        "edited": bool(edited_at),
                        "edited_at": edited_at,
                        "display_name": prof["display_name"],
                        "role": prof["role"],
                        "has_avatar": prof["has_avatar"],
                    }
                )
            return jsonify({"messages": msgs, "channel_id": ch["id"], "slug": slug})
        finally:
            conn.close()

    @app.route("/api/community/timeout/<wallet>")
    def api_timeout_status(wallet):
        wallet = _norm(wallet)
        conn = get_db()
        try:
            until = get_timeout_until(conn, wallet)
            now = int(time.time())
            return jsonify({
                "wallet": wallet,
                "timed_out": until > now,
                "until": until,
                "remaining": max(0, until - now),
            })
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
        # GIF posts: [[gif]]https://…tenor… or giphy
        if content.startswith("[[gif]]"):
            gif_url = content[7:].strip()
            low = gif_url.lower()
            ok_host = (
                low.startswith("https://")
                and (
                    "tenor.com" in low
                    or "tenor.co" in low
                    or "giphy.com" in low
                    or "giphy.gif" in low
                )
            )
            if not ok_host:
                return jsonify({"error": "GIF must be a Tenor/Giphy HTTPS URL"}), 400
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "You are banned from this community", "code": "banned"}), 403
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "not a member — join first", "code": "kicked"}), 403
            # Staff-only channels (e.g. #mods)
            if slug == "mods" and not is_staff(role):
                return jsonify({"error": "Mods channel is staff only", "code": "staff_only"}), 403
            now = int(time.time())
            until = get_timeout_until(conn, wallet)
            if until > now:
                mins = max(1, (until - now + 59) // 60)
                return jsonify({
                    "error": f"You are timed out for {mins} more minute(s)",
                    "code": "timed_out",
                    "until": until,
                    "remaining": until - now,
                }), 403

            ch = conn.execute(
                "SELECT * FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            # start-here: never chat. Otherwise readonly_members=1 → mods only;
            # readonly_members=0 → open chat (Gen Chat now; other channels when we flip).
            if ch["slug"] == "start-here":
                return jsonify({"error": "Start Here is info only — no chatting"}), 403

            # #media: anyone may post media-only; reject plain text / random links
            if ch["slug"] == "media":
                if not is_media_only_content(content):
                    return jsonify({
                        "error": "Media channel: upload an image or paste a GIF/video link",
                        "code": "media_only",
                    }), 400
            else:
                # Links / GIFs / images: admins + mods only (except #media above).
                # Staff may post [[img]]/[[gif]]/[[video]] in Gen Chat etc.
                if content_has_link(content) and not is_staff(role):
                    until = set_timeout(
                        conn,
                        wallet,
                        3600,
                        "Posted a link (admins/mods only)",
                        "system",
                    )
                    try:
                        socketio.emit(
                            "community_timeout",
                            {
                                "wallet": wallet,
                                "until": until,
                                "reason": "Posted a link (admins/mods only)",
                                "remaining": 3600,
                            },
                        )
                    except Exception:
                        pass
                    return jsonify({
                        "error": "Only admins and mods can post links. Timed out for 1 hour.",
                        "code": "link_timeout",
                        "until": until,
                        "remaining": 3600,
                    }), 403

            # Mentions: @everyone = staff only; anyone may @individuals
            mention_everyone, mention_wallets = parse_mentions(conn, content)
            if mention_everyone and not is_staff(role):
                return jsonify({
                    "error": "Only mods and admins can @everyone",
                    "code": "mention_everyone_denied",
                }), 403

            ro = int(ch["readonly_members"] or 0)
            if ch["slug"] != "media" and ro and not has_perm(role, "post_announcements") and role not in ("owner", "admin", "mod"):
                return jsonify({"error": "this channel is read-only — only mods can post"}), 403
            mid = str(uuid.uuid4())
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
                "mention_everyone": mention_everyone,
                "mention_wallets": mention_wallets,
            }
            try:
                socketio.emit("community_message", msg, room=f"community:{slug}")
            except Exception:
                pass
            if mention_everyone or mention_wallets:
                emit_community_mentions(
                    socketio, conn, msg, mention_everyone, mention_wallets, wallet
                )
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
    @app.route("/api/community/messages/<slug>/<msg_id>", methods=["PATCH", "PUT"])
    def api_channel_edit_msg(slug, msg_id):
        """Authors only: edit own message content."""
        data = request.json or {}
        wallet = _norm(data.get("wallet") or "")
        content = (data.get("content") or "").strip()
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not content or len(content) > 4000:
            return jsonify({"error": "content required (max 4000)"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            until = get_timeout_until(conn, wallet)
            now = int(time.time())
            if until > now:
                return jsonify({"error": "You are timed out", "code": "timed_out"}), 403
            if content_has_link(content) and not is_staff(role):
                until = set_timeout(
                    conn, wallet, 3600, "Edited in a link (admins/mods only)", "system"
                )
                return jsonify({
                    "error": "Only admins and mods can post links. Timed out for 1 hour.",
                    "code": "link_timeout",
                    "until": until,
                }), 403
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            row = conn.execute(
                "SELECT sender_wallet, content FROM community_messages WHERE id=? AND channel_id=?",
                (msg_id, ch["id"]),
            ).fetchone()
            if not row:
                return jsonify({"error": "message not found"}), 404
            if _norm(row["sender_wallet"]) != wallet:
                return jsonify({"error": "you can only edit your own messages"}), 403
            try:
                conn.execute(
                    "UPDATE community_messages SET content=?, edited_at=? WHERE id=? AND channel_id=?",
                    (content, now, msg_id, ch["id"]),
                )
            except Exception:
                conn.execute(
                    "UPDATE community_messages SET content=? WHERE id=? AND channel_id=?",
                    (content, msg_id, ch["id"]),
                )
            conn.commit()
            prof = profile_dict(conn, wallet)
            msg = {
                "id": msg_id,
                "sender_wallet": wallet,
                "content": content,
                "edited": True,
                "edited_at": now,
                "display_name": prof["display_name"],
                "role": prof["role"],
                "has_avatar": prof["has_avatar"],
                "slug": slug,
            }
            try:
                socketio.emit(
                    "community_message_edited",
                    msg,
                    room=f"community:{slug}",
                )
            except Exception:
                pass
            return jsonify({"ok": True, "message": msg})
        finally:
            conn.close()

    @app.route("/api/community/messages/<slug>/<msg_id>", methods=["DELETE"])
    def api_channel_delete_msg(slug, msg_id):
        data = request.json or {}
        wallet = _norm(data.get("wallet") or request.args.get("wallet", ""))
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            row = conn.execute(
                "SELECT sender_wallet, content FROM community_messages WHERE id=? AND channel_id=?",
                (msg_id, ch["id"]),
            ).fetchone()
            content_hint = (data.get("content") or "").strip()
            if not row:
                # May already be gone or P2P-only — still OK for client local delete
                return jsonify({"ok": True, "missing": True, "deleted_ids": [msg_id]})
            is_mod = role in ("owner", "admin", "mod") or has_perm(role, "delete_messages")
            is_author = _norm(row["sender_wallet"]) == wallet
            if not is_mod and not is_author:
                return jsonify({"error": "you can only delete your own messages"}), 403
            sender = _norm(row["sender_wallet"])
            content = row["content"] or content_hint
            deleted_ids = [msg_id]
            # Wipe exact content twins (same sender+body) so refresh doesn't resurrect dupes
            if content:
                twins = conn.execute(
                    """SELECT id FROM community_messages
                       WHERE channel_id=? AND lower(sender_wallet)=? AND content=? AND id!=?""",
                    (ch["id"], sender, content, msg_id),
                ).fetchall()
                for t in twins:
                    deleted_ids.append(t["id"])
                    conn.execute(
                        "DELETE FROM community_messages WHERE id=? AND channel_id=?",
                        (t["id"], ch["id"]),
                    )
            conn.execute(
                "DELETE FROM community_messages WHERE id=? AND channel_id=?",
                (msg_id, ch["id"]),
            )
            conn.commit()
            try:
                for did in deleted_ids:
                    socketio.emit(
                        "community_message_deleted",
                        {"id": did, "slug": slug, "content": content},
                        room=f"community:{slug}",
                    )
            except Exception:
                pass
            return jsonify({"ok": True, "deleted_ids": deleted_ids})
        finally:
            conn.close()

    @app.route("/api/community/channels/<slug>/mode", methods=["POST"])
    def api_channel_mode(slug):
        """Owner/admin: mode 'chat' (everyone) or 'post' (mods only). Growth flip."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        mode = (data.get("mode") or "").lower().strip()
        if mode not in ("chat", "post"):
            return jsonify({"error": "mode must be chat or post"}), 400
        if slug == "start-here":
            return jsonify({"error": "Start Here stays guide/info only"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if role not in ("owner", "admin"):
                return jsonify({"error": "owner/admin only"}), 403
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, slug),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            ro = 0 if mode == "chat" else 1
            conn.execute(
                "UPDATE community_channels SET readonly_members=? WHERE id=?",
                (ro, ch["id"]),
            )
            conn.commit()
            return jsonify({"ok": True, "slug": slug, "mode": mode})
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

    @app.route("/api/community/tickets", methods=["GET"])
    def api_tickets_list():
        wallet = _norm(request.args.get("wallet", ""))
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "banned", "code": "banned"}), 403
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "join first"}), 403
            if is_staff(role):
                rows = conn.execute(
                    """SELECT * FROM community_tickets WHERE community_id=?
                       ORDER BY created_at DESC LIMIT 100""",
                    (COMMUNITY_ID,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM community_tickets
                       WHERE community_id=? AND opener_wallet=?
                       ORDER BY created_at DESC LIMIT 50""",
                    (COMMUNITY_ID, wallet),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["opener"] = profile_dict(conn, r["opener_wallet"])
                out.append(d)
            return jsonify({"tickets": out, "count": len(out)})
        finally:
            conn.close()

    @app.route("/api/community/tickets", methods=["POST"])
    def api_tickets_create():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        subject = (data.get("subject") or "Help request")[:120].strip() or "Help request"
        body = (data.get("body") or data.get("content") or "").strip()[:4000]
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "banned", "code": "banned"}), 403
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "join first"}), 403
            until = get_timeout_until(conn, wallet)
            now = int(time.time())
            if until > now:
                return jsonify({"error": "You are timed out", "code": "timed_out"}), 403
            # One open ticket at a time for members
            if not is_staff(role):
                open_n = conn.execute(
                    """SELECT COUNT(*) AS c FROM community_tickets
                       WHERE community_id=? AND opener_wallet=? AND status='open'""",
                    (COMMUNITY_ID, wallet),
                ).fetchone()["c"]
                if open_n >= 1:
                    return jsonify({"error": "You already have an open ticket"}), 400
            tid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO community_tickets
                   (id, community_id, opener_wallet, subject, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (tid, COMMUNITY_ID, wallet, subject, "open", now),
            )
            if body:
                mid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO community_ticket_messages
                       (id, ticket_id, sender_wallet, content, created_at)
                       VALUES (?,?,?,?,?)""",
                    (mid, tid, wallet, body, now),
                )
            conn.commit()
            ticket = {
                "id": tid,
                "opener_wallet": wallet,
                "subject": subject,
                "status": "open",
                "created_at": now,
                "opener": profile_dict(conn, wallet),
            }
            try:
                socketio.emit("community_ticket_created", ticket, room="community_staff")
                socketio.emit("community_ticket_created", ticket, room=wallet)
            except Exception:
                pass
            return jsonify({"ok": True, "ticket": ticket})
        finally:
            conn.close()

    @app.route("/api/community/tickets/<tid>", methods=["GET"])
    def api_ticket_get(tid):
        wallet = _norm(request.args.get("wallet", ""))
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM community_tickets WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            t = dict(row)
            if not can_see_ticket(conn, t, wallet):
                return jsonify({"error": "forbidden"}), 403
            msgs = conn.execute(
                """SELECT id, sender_wallet, content, created_at FROM community_ticket_messages
                   WHERE ticket_id=? ORDER BY created_at ASC LIMIT 300""",
                (tid,),
            ).fetchall()
            out_msgs = []
            for m in msgs:
                prof = profile_dict(conn, m["sender_wallet"])
                out_msgs.append({
                    "id": m["id"],
                    "sender_wallet": m["sender_wallet"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                    "display_name": prof["display_name"],
                    "role": prof["role"],
                    "has_avatar": prof["has_avatar"],
                })
            t["opener"] = profile_dict(conn, t["opener_wallet"])
            t["messages"] = out_msgs
            return jsonify({"ticket": t})
        finally:
            conn.close()

    @app.route("/api/community/tickets/<tid>/messages", methods=["POST"])
    def api_ticket_message(tid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        content = (data.get("content") or "").strip()
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not content or len(content) > 4000:
            return jsonify({"error": "content required"}), 400
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM community_tickets WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            t = dict(row)
            if t.get("status") != "open":
                return jsonify({"error": "ticket is closed"}), 400
            if not can_see_ticket(conn, t, wallet):
                return jsonify({"error": "forbidden"}), 403
            role = ensure_member(conn, wallet)
            # Only opener or staff may reply
            is_opener = _norm(t["opener_wallet"]) == wallet
            if not is_opener and not is_staff(role):
                return jsonify({"error": "only the member or staff can reply"}), 403
            until = get_timeout_until(conn, wallet)
            now = int(time.time())
            if until > now and not is_staff(role):
                return jsonify({"error": "You are timed out", "code": "timed_out"}), 403
            # Links in tickets: staff OK; opener follows same link rule
            if content_has_link(content) and not is_staff(role):
                return jsonify({
                    "error": "Only admins and mods can post links. Ask staff to share links.",
                    "code": "link_blocked",
                }), 403
            mention_everyone, mention_wallets = parse_mentions(conn, content)
            if mention_everyone and not is_staff(role):
                return jsonify({
                    "error": "Only mods and admins can @everyone",
                    "code": "mention_everyone_denied",
                }), 403
            mid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO community_ticket_messages
                   (id, ticket_id, sender_wallet, content, created_at)
                   VALUES (?,?,?,?,?)""",
                (mid, tid, wallet, content, now),
            )
            conn.commit()
            prof = profile_dict(conn, wallet)
            msg = {
                "id": mid,
                "ticket_id": tid,
                "sender_wallet": wallet,
                "content": content,
                "created_at": now,
                "display_name": prof["display_name"],
                "role": prof["role"],
                "has_avatar": prof["has_avatar"],
                "slug": "ticket:" + tid,
                "mention_everyone": mention_everyone,
                "mention_wallets": mention_wallets,
            }
            try:
                socketio.emit("community_ticket_message", msg, room=f"ticket:{tid}")
                socketio.emit("community_ticket_message", msg, room="community_staff")
                socketio.emit("community_ticket_message", msg, room=_norm(t["opener_wallet"]))
            except Exception:
                pass
            if mention_everyone or mention_wallets:
                emit_community_mentions(
                    socketio, conn, msg, mention_everyone, mention_wallets, wallet
                )
            return jsonify({"ok": True, "message": msg})
        finally:
            conn.close()

    @app.route("/api/community/tickets/<tid>/close", methods=["POST"])
    def api_ticket_close(tid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM community_tickets WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            t = dict(row)
            if not can_see_ticket(conn, t, wallet):
                return jsonify({"error": "forbidden"}), 403
            role = get_role(conn, wallet)
            is_opener = _norm(t["opener_wallet"]) == wallet
            if not is_opener and not is_staff(role):
                return jsonify({"error": "forbidden"}), 403
            now = int(time.time())
            conn.execute(
                "UPDATE community_tickets SET status=?, closed_at=?, closed_by=? WHERE id=?",
                ("closed", now, wallet, tid),
            )
            conn.commit()
            try:
                socketio.emit(
                    "community_ticket_closed",
                    {"id": tid, "closed_by": wallet},
                    room=f"ticket:{tid}",
                )
            except Exception:
                pass
            return jsonify({"ok": True, "id": tid, "status": "closed"})
        finally:
            conn.close()

    @socketio.on("join_ticket")
    def on_join_ticket(data):
        data = data or {}
        tid = (data.get("ticket_id") or "").strip()
        wallet = _norm(data.get("wallet", ""))
        if not wallet.startswith("0x"):
            return
        conn = get_db()
        try:
            role = get_role(conn, wallet) or ensure_member(conn, wallet)
            if is_staff(role):
                join_room("community_staff")
            if not tid:
                return
            row = conn.execute(
                "SELECT * FROM community_tickets WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row or not can_see_ticket(conn, dict(row), wallet):
                return
            join_room(f"ticket:{tid}")
        finally:
            conn.close()

    @app.route("/api/community/threads", methods=["GET"])
    def api_threads_list():
        """List threads for a channel visible to this wallet."""
        wallet = _norm(request.args.get("wallet", ""))
        channel = (request.args.get("channel") or request.args.get("slug") or "").lower().strip()
        if not wallet.startswith("0x") or not channel:
            return jsonify({"error": "wallet and channel required"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "join first"}), 403
            rows = conn.execute(
                """SELECT * FROM community_threads
                   WHERE community_id=? AND channel_slug=? AND status='open'
                   ORDER BY created_at DESC LIMIT 80""",
                (COMMUNITY_ID, channel),
            ).fetchall()
            out = []
            for r in rows:
                tid = r["id"]
                if not can_see_thread(conn, tid, wallet):
                    continue
                members = conn.execute(
                    "SELECT wallet FROM community_thread_members WHERE thread_id=?",
                    (tid,),
                ).fetchall()
                out.append({
                    "id": tid,
                    "channel_slug": r["channel_slug"],
                    "title": r["title"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"],
                    "status": r["status"],
                    "members": [m["wallet"] for m in members],
                    "creator": profile_dict(conn, r["created_by"]),
                })
            return jsonify({"threads": out, "count": len(out)})
        finally:
            conn.close()

    @app.route("/api/community/threads", methods=["POST"])
    def api_threads_create():
        """Admin/owner creates an invite-only thread under a channel."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        channel = (data.get("channel") or data.get("slug") or "").lower().strip()
        title = (data.get("title") or "Thread")[:120].strip() or "Thread"
        invitees = data.get("members") or data.get("invite") or []
        if isinstance(invitees, str):
            invitees = [x.strip() for x in invitees.replace(";", ",").split(",") if x.strip()]
        invitees = [_norm(x) for x in invitees if str(x).startswith("0x")]
        if not wallet.startswith("0x") or not channel:
            return jsonify({"error": "wallet and channel required"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if role not in ("owner", "admin"):
                return jsonify({"error": "admins only can create threads"}), 403
            ch = conn.execute(
                "SELECT id FROM community_channels WHERE community_id=? AND slug=?",
                (COMMUNITY_ID, channel),
            ).fetchone()
            if not ch:
                return jsonify({"error": "channel not found"}), 404
            tid = str(uuid.uuid4())
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_threads
                   (id, community_id, channel_slug, title, created_by, created_at, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid, COMMUNITY_ID, channel, title, wallet, now, "open"),
            )
            # Creator always a member; plus tagged wallets
            members = {wallet}
            for w in invitees:
                if w.startswith("0x") and len(w) == 42:
                    members.add(w)
            for w in members:
                conn.execute(
                    """INSERT OR IGNORE INTO community_thread_members
                       (thread_id, wallet, added_by, created_at) VALUES (?,?,?,?)""",
                    (tid, w, wallet, now),
                )
            conn.commit()
            ticket = {
                "id": tid,
                "channel_slug": channel,
                "title": title,
                "created_by": wallet,
                "created_at": now,
                "status": "open",
                "members": list(members),
            }
            try:
                for w in members:
                    socketio.emit("community_thread_created", ticket, room=w)
            except Exception:
                pass
            return jsonify({"ok": True, "thread": ticket})
        finally:
            conn.close()

    @app.route("/api/community/threads/<tid>/members", methods=["POST"])
    def api_thread_add_member(tid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        target = _norm(data.get("target", ""))
        tid = (tid or "").strip()
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if role not in ("owner", "admin"):
                return jsonify({"error": "admins only"}), 403
            row = conn.execute(
                "SELECT * FROM community_threads WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            if not target.startswith("0x"):
                return jsonify({"error": "target wallet required"}), 400
            now = int(time.time())
            conn.execute(
                """INSERT OR IGNORE INTO community_thread_members
                   (thread_id, wallet, added_by, created_at) VALUES (?,?,?,?)""",
                (tid, target, wallet, now),
            )
            conn.commit()
            try:
                socketio.emit(
                    "community_thread_invited",
                    {"thread_id": tid, "title": row["title"], "channel_slug": row["channel_slug"]},
                    room=target,
                )
            except Exception:
                pass
            return jsonify({"ok": True, "thread_id": tid, "target": target})
        finally:
            conn.close()

    @app.route("/api/community/threads/<tid>", methods=["GET"])
    def api_thread_get(tid):
        wallet = _norm(request.args.get("wallet", ""))
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM community_threads WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            if not can_see_thread(conn, tid, wallet):
                return jsonify({"error": "forbidden"}), 403
            msgs = conn.execute(
                """SELECT id, sender_wallet, content, created_at FROM community_thread_messages
                   WHERE thread_id=? ORDER BY created_at ASC LIMIT 300""",
                (tid,),
            ).fetchall()
            members = conn.execute(
                "SELECT wallet FROM community_thread_members WHERE thread_id=?",
                (tid,),
            ).fetchall()
            out_msgs = []
            for m in msgs:
                prof = profile_dict(conn, m["sender_wallet"])
                out_msgs.append({
                    "id": m["id"],
                    "sender_wallet": m["sender_wallet"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                    "display_name": prof["display_name"],
                    "role": prof["role"],
                })
            t = dict(row)
            t["messages"] = out_msgs
            t["members"] = [m["wallet"] for m in members]
            t["creator"] = profile_dict(conn, row["created_by"])
            return jsonify({"thread": t})
        finally:
            conn.close()

    @app.route("/api/community/threads/<tid>/messages", methods=["POST"])
    def api_thread_message(tid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        content = (data.get("content") or "").strip()
        if not wallet.startswith("0x") or not content or len(content) > 4000:
            return jsonify({"error": "wallet and content required"}), 400
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM community_threads WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            if row["status"] != "open":
                return jsonify({"error": "thread closed"}), 400
            if not can_see_thread(conn, tid, wallet):
                return jsonify({"error": "forbidden"}), 403
            role = ensure_member(conn, wallet)
            if content_has_link(content) and not is_staff(role):
                return jsonify({"error": "Only admins/mods can post links"}), 403
            mention_everyone, mention_wallets = parse_mentions(conn, content)
            if mention_everyone and not is_staff(role):
                return jsonify({
                    "error": "Only mods and admins can @everyone",
                    "code": "mention_everyone_denied",
                }), 403
            mid = str(uuid.uuid4())
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_thread_messages
                   (id, thread_id, sender_wallet, content, created_at) VALUES (?,?,?,?,?)""",
                (mid, tid, wallet, content, now),
            )
            conn.commit()
            prof = profile_dict(conn, wallet)
            msg = {
                "id": mid,
                "thread_id": tid,
                "sender_wallet": wallet,
                "content": content,
                "created_at": now,
                "display_name": prof["display_name"],
                "role": prof["role"],
                "slug": "thread:" + tid,
                "mention_everyone": mention_everyone,
                "mention_wallets": mention_wallets,
            }
            try:
                members = conn.execute(
                    "SELECT wallet FROM community_thread_members WHERE thread_id=?",
                    (tid,),
                ).fetchall()
                socketio.emit("community_thread_message", msg, room=f"thread:{tid}")
                for m in members:
                    socketio.emit("community_thread_message", msg, room=m["wallet"])
            except Exception:
                pass
            if mention_everyone or mention_wallets:
                # Prefer alerting tagged thread members; @everyone still expands to community
                targets = mention_wallets
                if not mention_everyone:
                    member_set = {_norm(m["wallet"]) for m in members}
                    targets = [w for w in mention_wallets if w in member_set] or mention_wallets
                emit_community_mentions(
                    socketio, conn, msg, mention_everyone, targets, wallet
                )
            return jsonify({"ok": True, "message": msg})
        finally:
            conn.close()

    @app.route("/api/community/threads/<tid>/close", methods=["POST"])
    def api_thread_close(tid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if role not in ("owner", "admin"):
                return jsonify({"error": "admins only"}), 403
            conn.execute(
                "UPDATE community_threads SET status='closed' WHERE id=? AND community_id=?",
                (tid, COMMUNITY_ID),
            )
            conn.commit()
            return jsonify({"ok": True, "id": tid, "status": "closed"})
        finally:
            conn.close()

    @socketio.on("join_thread")
    def on_join_thread(data):
        data = data or {}
        tid = (data.get("thread_id") or "").strip()
        wallet = _norm(data.get("wallet", ""))
        if not tid or not wallet.startswith("0x"):
            return
        conn = get_db()
        try:
            if can_see_thread(conn, tid, wallet):
                join_room(f"thread:{tid}")
        finally:
            conn.close()

    # ── Garden bot (shared grow-a-tree) + external bot webhook ──

    @app.route("/api/community/garden")
    def api_garden_status():
        wallet = _norm(request.args.get("wallet", ""))
        conn = get_db()
        try:
            return jsonify(garden_state_dict(conn, viewer=wallet))
        finally:
            conn.close()

    @app.route("/api/community/garden/water", methods=["POST"])
    def api_garden_water():
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "banned", "code": "banned"}), 403
            ensure_member(conn, wallet)
            garden_ensure_columns(conn)
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_garden (community_id, xp, waters, stage, updated_at, last_milestone)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(community_id) DO NOTHING""",
                (COMMUNITY_ID, 0, 0, 0, 0, 0),
            )
            g = conn.execute(
                "SELECT * FROM community_garden WHERE community_id=?",
                (COMMUNITY_ID,),
            ).fetchone()
            old_size = int(g["waters"] or 0) or int(g["xp"] or 0)
            last_at = int(g["updated_at"] or 0)
            try:
                last_w = _norm(g["last_waterer"] or "")
            except Exception:
                last_w = ""
            # Discord: same person cannot water twice in a row
            if last_w and last_w == wallet and old_size > 0:
                st = garden_state_dict(conn, viewer=wallet)
                return jsonify({
                    "error": "You watered this tree last — someone else has to water it first.",
                    "code": "same_waterer",
                    "garden": st,
                }), 429
            cd = garden_cooldown_for_size(max(1, old_size))
            left = max(0, (last_at + cd) - now) if last_at else 0
            if left > 0:
                st = garden_state_dict(conn, viewer=wallet)
                return jsonify({
                    "error": f"Tree is still growing — waterable in {left}s",
                    "code": "cooldown",
                    "cooldown_remaining": left,
                    "garden": st,
                }), 429
            new_size = old_size + 1
            new_xp = int(g["xp"] or 0) + 1
            if new_xp < new_size:
                new_xp = new_size
            old_stage, _, _, _ = garden_stage_for_size(old_size)
            new_stage, emoji, label, _art = garden_stage_for_size(new_size)
            conn.execute(
                """UPDATE community_garden
                   SET xp=?, waters=?, stage=?, updated_at=?, last_waterer=?
                   WHERE community_id=?""",
                (new_xp, new_size, new_stage, now, wallet, COMMUNITY_ID),
            )
            wr = conn.execute(
                """SELECT last_water_at, total_waters FROM community_garden_waters
                   WHERE community_id=? AND wallet=?""",
                (COMMUNITY_ID, wallet),
            ).fetchone()
            if wr:
                conn.execute(
                    """UPDATE community_garden_waters
                       SET last_water_at=?, total_waters=total_waters+1
                       WHERE community_id=? AND wallet=?""",
                    (now, COMMUNITY_ID, wallet),
                )
            else:
                conn.execute(
                    """INSERT INTO community_garden_waters
                       (community_id, wallet, last_water_at, total_waters)
                       VALUES (?,?,?,?)""",
                    (COMMUNITY_ID, wallet, now, 1),
                )
            garden_log_event(
                conn,
                "water",
                wallet=wallet,
                size=new_size,
                detail=f"stage={new_stage}:{label}",
            )
            conn.commit()
            prof = profile_dict(conn, wallet)
            who = (prof.get("display_name") or "").strip() or (
                wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet
            )
            # Celebrate stage-ups; light pings every 10 waters
            if new_stage > old_stage:
                post_channel_bot_message(
                    conn,
                    socketio,
                    "garden",
                    f"{emoji} **{label}!** The tree is now **{new_size}ft** — thanks {who} and everyone watering!",
                    GARDEN_BOT_NAME,
                )
            elif new_size % 10 == 0:
                post_channel_bot_message(
                    conn,
                    socketio,
                    "garden",
                    f"{emoji} {who} watered the tree · **{new_size}ft** · {label}",
                    GARDEN_BOT_NAME,
                )
            # Fruit harvest: chance rises with height (Discord-style apple drops)
            apple = None
            chance = 0.22 if new_size >= 5 else (0.12 if new_size >= 2 else 0.0)
            if chance and random.random() < chance:
                apple = garden_spawn_apple(socketio)
                if apple:
                    post_channel_bot_message(
                        conn,
                        socketio,
                        "garden",
                        "🍎 Fruit dropping! Tap the basket under the apple to catch it.",
                        GARDEN_BOT_NAME,
                    )
            st = garden_state_dict(conn, viewer=wallet)
            # Waterer just watered — they can't water again until someone else does
            st["can_water"] = False
            st["blocked_reason"] = "same_waterer"
            st["cooldown_remaining"] = st.get("cooldown_sec") or cd
            try:
                socketio.emit("community_garden", st, room="community:garden")
            except Exception:
                pass
            return jsonify({
                "ok": True,
                "garden": st,
                "grew": new_stage > old_stage,
                "height_ft": new_size,
                "apple_drop": apple,
            })
        finally:
            conn.close()

    @app.route("/api/community/garden/catch", methods=["POST"])
    def api_garden_catch():
        """Catch a falling apple: pick the basket lane under the fruit."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        drop_id = (data.get("drop_id") or data.get("id") or "").strip()
        try:
            lane = int(data.get("lane"))
        except Exception:
            lane = -1
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not drop_id:
            return jsonify({"error": "drop_id required"}), 400
        garden_prune_apple_drops()
        drop = GARDEN_APPLE_DROPS.get(drop_id)
        if not drop:
            return jsonify({"error": "Apple already gone", "code": "gone"}), 410
        if drop.get("caught"):
            return jsonify({
                "error": "Already caught",
                "code": "caught",
                "by": drop.get("by_name") or drop.get("by"),
            }), 409
        if float(drop.get("exp") or 0) < time.time():
            GARDEN_APPLE_DROPS.pop(drop_id, None)
            return jsonify({"error": "Too slow — apple hit the ground", "code": "expired"}), 410
        if lane != int(drop.get("lane", -1)):
            return jsonify({
                "error": "Wrong basket — apple missed!",
                "code": "miss",
                "lane": drop.get("lane"),
            }), 400
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "banned", "code": "banned"}), 403
            ensure_member(conn, wallet)
            garden_ensure_columns(conn)
            prof = profile_dict(conn, wallet)
            who = (prof.get("display_name") or "").strip() or (
                wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet
            )
            drop["caught"] = True
            drop["by"] = wallet
            drop["by_name"] = who
            conn.execute(
                """INSERT INTO community_garden (community_id, xp, waters, stage, updated_at, last_milestone)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(community_id) DO NOTHING""",
                (COMMUNITY_ID, 0, 0, 0, int(time.time()), 0),
            )
            conn.execute(
                """UPDATE community_garden SET apples=COALESCE(apples,0)+1
                   WHERE community_id=?""",
                (COMMUNITY_ID,),
            )
            wr = conn.execute(
                """SELECT total_waters FROM community_garden_waters
                   WHERE community_id=? AND wallet=?""",
                (COMMUNITY_ID, wallet),
            ).fetchone()
            if wr:
                conn.execute(
                    """UPDATE community_garden_waters
                       SET apples=COALESCE(apples,0)+1
                       WHERE community_id=? AND wallet=?""",
                    (COMMUNITY_ID, wallet),
                )
            else:
                conn.execute(
                    """INSERT INTO community_garden_waters
                       (community_id, wallet, last_water_at, total_waters, apples)
                       VALUES (?,?,?,?,?)""",
                    (COMMUNITY_ID, wallet, 0, 0, 1),
                )
            gsize = conn.execute(
                "SELECT waters FROM community_garden WHERE community_id=?",
                (COMMUNITY_ID,),
            ).fetchone()
            garden_log_event(
                conn,
                "catch",
                wallet=wallet,
                size=int(gsize["waters"] or 0) if gsize else 0,
                detail="apple",
            )
            conn.commit()
            post_channel_bot_message(
                conn,
                socketio,
                "garden",
                f"🍎 {who} caught an apple!",
                GARDEN_BOT_NAME,
            )
            st = garden_state_dict(conn, viewer=wallet)
            try:
                socketio.emit(
                    "community_garden_apple",
                    {**drop, "caught": True, "by": wallet, "by_name": who},
                    room="community:garden",
                )
                socketio.emit("community_garden", st, room="community:garden")
            except Exception:
                pass
            return jsonify({"ok": True, "garden": st, "caught": True, "by_name": who})
        finally:
            conn.close()

    @app.route("/api/community/garden/restore", methods=["POST"])
    def api_garden_restore():
        """Staff: set tree height after accidental wipe. Body: { wallet, waters }."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        try:
            waters = int(data.get("waters") or data.get("height_ft") or 0)
        except Exception:
            waters = 0
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if waters < 0 or waters > 100000:
            return jsonify({"error": "waters out of range"}), 400
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if not is_staff(role):
                return jsonify({"error": "staff only"}), 403
            garden_ensure_columns(conn)
            now = int(time.time())
            stage_i, emoji, label, _art = garden_stage_for_size(waters)
            conn.execute(
                """INSERT INTO community_garden (community_id, xp, waters, stage, updated_at, last_milestone)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(community_id) DO UPDATE SET
                     xp=excluded.xp, waters=excluded.waters, stage=excluded.stage,
                     updated_at=excluded.updated_at""",
                (COMMUNITY_ID, max(waters, 0), waters, stage_i, now, 0),
            )
            # Don't set last_waterer so anyone can water next
            conn.execute(
                """UPDATE community_garden SET last_waterer='' WHERE community_id=?""",
                (COMMUNITY_ID,),
            )
            garden_log_event(
                conn,
                "restore",
                wallet=wallet,
                size=waters,
                detail=f"staff restore → {label}",
            )
            conn.commit()
            post_channel_bot_message(
                conn,
                socketio,
                "garden",
                f"{emoji} Tree height restored to **{waters}ft** ({label}) by staff.",
                GARDEN_BOT_NAME,
            )
            st = garden_state_dict(conn, viewer=wallet)
            try:
                socketio.emit("community_garden", st, room="community:garden")
            except Exception:
                pass
            return jsonify({"ok": True, "garden": st})
        finally:
            conn.close()

    @app.route("/api/community/bots/message", methods=["POST"])
    def api_bot_message():
        """External bots: POST with header X-LightChat-Bot-Token.
        Body: { channel, content, display_name? }
        """
        token = (
            request.headers.get("X-LightChat-Bot-Token")
            or request.headers.get("Authorization", "").replace("Bearer", "").strip()
        )
        data = request.json or {}
        content = (data.get("content") or "").strip()
        channel = (data.get("channel") or data.get("slug") or "garden").strip().lower()
        bot_name = (data.get("display_name") or data.get("name") or "Bot").strip()[:40] or "Bot"
        if not token or not content or len(content) > 4000:
            return jsonify({"error": "token + content required (max 4000)"}), 400
        if channel in ("start-here", "mods") and channel == "start-here":
            return jsonify({"error": "cannot post to this channel"}), 403
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT name FROM community_bot_tokens WHERE token=? AND community_id=?",
                (token, COMMUNITY_ID),
            ).fetchone()
            # Allow env fallback for handoff demos
            env_tok = (os.environ.get("LIGHTCHAT_BOT_TOKEN") or "").strip()
            if not row and not (env_tok and token == env_tok):
                return jsonify({"error": "invalid bot token"}), 401
            if row:
                bot_name = row["name"] or bot_name
                conn.execute(
                    "UPDATE community_bot_tokens SET last_used_at=? WHERE token=?",
                    (int(time.time()), token),
                )
                conn.commit()
            if channel == "start-here":
                return jsonify({"error": "cannot post to start-here"}), 403
            msg = post_channel_bot_message(conn, socketio, channel, content, bot_name)
            if not msg:
                return jsonify({"error": "channel not found"}), 404
            return jsonify({"ok": True, "message": msg})
        finally:
            conn.close()

    @app.route("/api/community/bots/tokens", methods=["POST"])
    def api_bot_create_token():
        """Staff: create a bot token for external integrations."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        name = (data.get("name") or "Community Bot").strip()[:40] or "Community Bot"
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            if not is_staff(role):
                return jsonify({"error": "staff only"}), 403
            token = "lcb_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_bot_tokens
                   (token, name, community_id, created_by, created_at, last_used_at)
                   VALUES (?,?,?,?,?,?)""",
                (token, name, COMMUNITY_ID, wallet, now, 0),
            )
            conn.commit()
            return jsonify({"ok": True, "token": token, "name": name})
        finally:
            conn.close()

    @app.route("/api/community/stickers")
    def api_stickers_list():
        kind = (request.args.get("kind") or "").lower().strip()
        conn = get_db()
        try:
            if kind in ("sticker", "emoji"):
                rows = conn.execute(
                    """SELECT id, kind, name, image_type, uploader, created_at
                       FROM community_stickers WHERE community_id=? AND kind=?
                       ORDER BY created_at DESC LIMIT 200""",
                    (COMMUNITY_ID, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, kind, name, image_type, uploader, created_at
                       FROM community_stickers WHERE community_id=?
                       ORDER BY created_at DESC LIMIT 200""",
                    (COMMUNITY_ID,),
                ).fetchall()
            out = []
            for r in rows:
                out.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "image_type": r["image_type"],
                    "uploader": r["uploader"],
                    "created_at": r["created_at"],
                    "url": f"/api/community/stickers/{r['id']}",
                })
            return jsonify({"stickers": out, "count": len(out)})
        finally:
            conn.close()

    @app.route("/api/community/stickers", methods=["POST"])
    def api_stickers_upload():
        """Any member can upload a sticker/emoji (max ~500KB)."""
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        name = (data.get("name") or "sticker")[:40].strip() or "sticker"
        kind = (data.get("kind") or "sticker").lower().strip()
        if kind not in ("sticker", "emoji"):
            kind = "sticker"
        raw = data.get("image_data") or ""
        itype = (data.get("image_type") or "image/png")[:40]
        if not wallet.startswith("0x"):
            return jsonify({"error": "wallet required"}), 400
        if not raw or len(raw) > 700_000:
            return jsonify({"error": "image missing or too large (max ~500KB)"}), 400
        if "," in raw[:80]:
            raw = raw.split(",", 1)[1]
        conn = get_db()
        try:
            if is_banned(conn, wallet):
                return jsonify({"error": "banned", "code": "banned"}), 403
            role = ensure_member(conn, wallet)
            if not role:
                return jsonify({"error": "join community first"}), 403
            # Cap pack size
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM community_stickers WHERE community_id=?",
                (COMMUNITY_ID,),
            ).fetchone()["c"]
            if n >= 300:
                return jsonify({"error": "server sticker pack is full (300)"}), 400
            sid = str(uuid.uuid4())
            now = int(time.time())
            conn.execute(
                """INSERT INTO community_stickers
                   (id, community_id, kind, name, image_data, image_type, uploader, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (sid, COMMUNITY_ID, kind, name, raw, itype, wallet, now),
            )
            conn.commit()
            return jsonify({
                "ok": True,
                "sticker": {
                    "id": sid,
                    "kind": kind,
                    "name": name,
                    "url": f"/api/community/stickers/{sid}",
                },
            })
        finally:
            conn.close()

    @app.route("/api/community/stickers/<sid>")
    def api_sticker_image(sid):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT image_data, image_type FROM community_stickers WHERE id=? AND community_id=?",
                (sid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            try:
                body = base64.b64decode(row["image_data"])
            except Exception:
                return jsonify({"error": "bad image"}), 500
            return Response(body, mimetype=row["image_type"] or "image/png")
        finally:
            conn.close()

    @app.route("/api/community/stickers/<sid>", methods=["DELETE"])
    def api_sticker_delete(sid):
        data = request.json or {}
        wallet = _norm(data.get("wallet", ""))
        conn = get_db()
        try:
            role = ensure_member(conn, wallet)
            row = conn.execute(
                "SELECT uploader FROM community_stickers WHERE id=? AND community_id=?",
                (sid, COMMUNITY_ID),
            ).fetchone()
            if not row:
                return jsonify({"error": "not found"}), 404
            is_uploader = _norm(row["uploader"]) == wallet
            if not is_uploader and not is_staff(role):
                return jsonify({"error": "only uploader or mods can delete"}), 403
            conn.execute(
                "DELETE FROM community_stickers WHERE id=? AND community_id=?",
                (sid, COMMUNITY_ID),
            )
            conn.commit()
            return jsonify({"ok": True})
        finally:
            conn.close()

    @socketio.on("join_community_channel")
    def on_join_community(data):
        slug = (data or {}).get("slug") or "general"
        join_room(f"community:{slug}")

    @socketio.on("set_presence")
    def on_set_presence(data):
        """Discord-style: online | invisible. Persists preference + live status."""
        from flask import request as flask_request
        from flask_socketio import emit as sio_emit

        data = data or {}
        wallet = _norm(data.get("wallet", ""))
        mode = (data.get("mode") or "online").lower().strip()
        if mode not in ("online", "invisible"):
            sio_emit("error", {"message": "mode must be online or invisible"})
            return
        if not wallet.startswith("0x"):
            return
        # Prefer authenticated socket wallet if available
        try:
            # sid mapped in server._socket_wallets — optional trust wallet from client if matches
            pass
        except Exception:
            pass
        presence_set(wallet, mode, flask_request.sid)
        conn = get_db()
        try:
            ensure_member(conn, wallet)
            row = conn.execute(
                "SELECT wallet FROM community_profiles WHERE wallet=?", (wallet,)
            ).fetchone()
            now = int(time.time())
            if row:
                conn.execute(
                    "UPDATE community_profiles SET presence_mode=?, updated_at=? WHERE wallet=?",
                    (mode, now, wallet),
                )
            else:
                conn.execute(
                    """INSERT INTO community_profiles
                       (wallet, display_name, bio, hide_wallet, presence_mode, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (wallet, "", "", 0, mode, now),
                )
            conn.commit()
        finally:
            conn.close()
        visible = presence_is_visible_online(wallet)
        sio_emit("presence_ok", {"wallet": wallet, "mode": mode, "online": visible})
        socketio.emit(
            "presence_update",
            {"wallet": wallet, "online": visible, "mode": mode},
            skip_sid=None,
        )

    print("  [community] routes registered — official Lightchain server ready")


def presence_on_auth(wallet: str, sid: str, get_db, socketio) -> None:
    """Call from server auth — mark wallet present using saved Invisible preference."""
    wallet = _norm(wallet)
    if not wallet.startswith("0x"):
        return
    conn = get_db()
    try:
        mode = presence_get_mode(conn, wallet)
        ensure_member(conn, wallet)
    finally:
        conn.close()
    presence_set(wallet, mode, sid)
    try:
        socketio.emit(
            "presence_update",
            {
                "wallet": wallet,
                "online": presence_is_visible_online(wallet),
                "mode": mode,
            },
        )
    except Exception:
        pass


def presence_on_disconnect(wallet: str, sid: str, socketio) -> None:
    wallet = _norm(wallet)
    if not presence_clear(wallet, sid):
        return
    try:
        socketio.emit(
            "presence_update",
            {"wallet": wallet, "online": False, "mode": "offline"},
        )
    except Exception:
        pass
