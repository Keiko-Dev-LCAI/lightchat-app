#!/usr/bin/env python3
"""Ingest KNOWLEDGE_SOURCES into knowledge_chunks (Phase 2 AIVM RAG).

Usage (on the host with the same DATA_DIR / DB as the app):
  export KNOWLEDGE_SOURCES='https://example.com/whitepaper,https://example.com/docs'
  python ingest_knowledge.py

Idempotent per source_url (clears + reinserts that URL's chunks).
Requires KNOWLEDGE_SOURCES; exits 0 with a note if unset.
"""
from __future__ import annotations

import os
import sys

# Allow running from repo root or /app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    # Reuse server get_db if available; else open sqlite at DATA_DIR
    try:
        from server import get_db
    except Exception:
        import sqlite3

        data_dir = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
        db_path = os.environ.get("DB_PATH") or os.path.join(data_dir, "lightchat.db")
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

        def get_db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

    from community import (
        init_community_db,
        ingest_all_knowledge,
        knowledge_sources_from_env,
    )

    sources = knowledge_sources_from_env()
    if not sources:
        print("KNOWLEDGE_SOURCES unset — nothing to ingest (Phase 2 dormant).")
        return 0

    init_community_db(get_db)
    conn = get_db()
    try:
        result = ingest_all_knowledge(conn)
        print(f"Ingested {result.get('chunks', 0)} chunks from {result.get('sources', 0)} sources:")
        for url, n in (result.get("per_source") or {}).items():
            print(f"  {n:4d}  {url}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
