"""SQLite store for tenders (allow-listed fields only), cached extractions and sent alerts."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / ".tmp" / "tenders.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,          -- JSON of the allow-listed record
    close_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
    doc_url TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    tender_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (tender_id, profile)
);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect(path=DB_PATH, shared=False):
    """shared=True lets a web app reuse one connection across threads (callers must serialise access)."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=not shared)
    db.executescript(SCHEMA)
    return db


def upsert_tenders(db, tenders):
    """Insert or refresh tenders. Returns the ids that were not seen before."""
    ts = now()
    known = {r[0] for r in db.execute("SELECT id FROM tenders")}
    new = []
    for t in tenders:
        if t["id"] not in known:
            new.append(t["id"])
        db.execute(
            """INSERT INTO tenders (id, data, close_at, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET data=excluded.data, close_at=excluded.close_at, last_seen=excluded.last_seen""",
            (t["id"], json.dumps(t), t.get("close_at"), ts, ts),
        )
    db.commit()
    return new


def open_tenders(db, today=None):
    """Tenders that have not closed yet, soonest deadline first."""
    today = (today or datetime.now()).strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT data, first_seen FROM tenders WHERE close_at >= ? ORDER BY close_at", (today,)
    ).fetchall()
    return [dict(json.loads(d), first_seen=fs) for d, fs in rows]


def closing_within(db, days, today=None):
    today = today or datetime.now()
    limit = (today + timedelta(days=days)).strftime("%Y-%m-%d 23:59:59")
    return [t for t in open_tenders(db, today) if t["close_at"] <= limit]


def get_extraction(db, doc_url):
    row = db.execute("SELECT data FROM extractions WHERE doc_url = ?", (doc_url,)).fetchone()
    return json.loads(row[0]) if row else None


def save_extraction(db, doc_url, data):
    db.execute(
        "INSERT OR REPLACE INTO extractions (doc_url, data, created) VALUES (?, ?, ?)",
        (doc_url, json.dumps(data), now()),
    )
    db.commit()


def export_extractions(db, doc_urls):
    """{doc_url: extraction} for the given documents (used to publish a shared warm cache)."""
    out = {}
    for url in doc_urls:
        if url and (e := get_extraction(db, url)):
            out[url] = e
    return out


def import_extractions(db, data):
    """Load a published warm cache. Existing local entries win. Returns how many were added."""
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO extractions (doc_url, data, created) VALUES (?, ?, ?)",
        [(url, json.dumps(e), now()) for url, e in data.items() if isinstance(e, dict) and e.get("status")],
    )
    db.commit()
    return db.total_changes - before


def already_sent(db, profile):
    return {r[0] for r in db.execute("SELECT tender_id FROM sent WHERE profile = ?", (profile,))}


def mark_sent(db, profile, tender_ids):
    ts = now()
    db.executemany(
        "INSERT OR IGNORE INTO sent (tender_id, profile, sent_at) VALUES (?, ?, ?)",
        [(i, profile, ts) for i in tender_ids],
    )
    db.commit()
