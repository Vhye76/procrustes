import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("state")

DETECTED = "DETECTED"
PROBED = "PROBED"
SCREENED = "SCREENED"
IDENTIFIED = "IDENTIFIED"
COMPARED = "COMPARED"
ROUTED = "ROUTED"
STAGED = "STAGED"
REMUXED = "REMUXED"
TAGGED = "TAGGED"
READY = "READY"
ENCODING = "ENCODING"
ENCODED = "ENCODED"
VERIFIED = "VERIFIED"
PUBLISHED = "PUBLISHED"
CLEANUP = "CLEANUP"

HELD = "HELD"
QUARANTINED = "QUARANTINED"
FAILED = "FAILED"

PIPELINE = (
    DETECTED, PROBED, SCREENED, IDENTIFIED, COMPARED, ROUTED, STAGED, REMUXED,
    TAGGED, READY, ENCODING, ENCODED, VERIFIED, PUBLISHED, CLEANUP,
)
ASSESSMENT = (DETECTED, PROBED, SCREENED, IDENTIFIED, COMPARED)
COMPLETE = (PUBLISHED, CLEANUP)
TERMINAL = (CLEANUP, QUARANTINED)
STOPPED = (HELD, QUARANTINED, FAILED)

DISPLAY_NAMES = {
    DETECTED: "queued",
    PROBED: "probed",
    SCREENED: "screened",
    IDENTIFIED: "identified",
    COMPARED: "compared",
    ROUTED: "waiting for encoder",
    STAGED: "copying",
    REMUXED: "remuxed",
    TAGGED: "tagged",
    READY: "ready",
    ENCODING: "encoding",
    ENCODED: "encoded",
    VERIFIED: "verified",
    PUBLISHED: "ready to promote",
    CLEANUP: "ready to promote",
    HELD: "needs a decision",
    QUARANTINED: "rejected",
    FAILED: "failed",
}


def display_name(stage):
    return DISPLAY_NAMES.get(stage, (stage or "").lower())


def is_complete(stage):
    return stage in COMPLETE


def in_pipeline(stage):
    return stage != QUARANTINED


def files_present(row):
    return {
        name: bool(row.get(key)) and os.path.exists(row[key])
        for name, key in (
            ("output", "output_path"),
            ("source", "source_path"),
            ("quarantine", "quarantine_path"),
        )
    }


#----- Schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path   TEXT NOT NULL UNIQUE,
    kind          TEXT,
    stage         TEXT NOT NULL,
    title         TEXT,
    year          INTEGER,
    show          TEXT,
    season        INTEGER,
    episode       INTEGER,
    tmdb          TEXT,
    imdb          TEXT,
    tvdb          TEXT,
    job_id        TEXT,
    work_path     TEXT,
    output_path   TEXT,
    quarantine_path TEXT,
    origin_path   TEXT,
    poster_url    TEXT,
    encoder       TEXT,
    grain_ratio   REAL,
    probe_json    TEXT,
    identity_json TEXT,
    decision_json TEXT,
    compare_json  TEXT,
    output_probe_json TEXT,
    reason        TEXT,
    reasons_json  TEXT,
    candidates_json TEXT,
    pinned_json   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    retry_after   REAL,
    overridden    INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS titles_stage ON titles(stage);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id  INTEGER NOT NULL,
    stage     TEXT NOT NULL,
    detail    TEXT,
    at        REAL NOT NULL,
    FOREIGN KEY (title_id) REFERENCES titles(id)
);
CREATE INDEX IF NOT EXISTS history_title ON history(title_id);

CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    kind          TEXT,
    size          INTEGER,
    mtime         REAL,
    checks_json   TEXT,
    measured_json TEXT,
    summary       TEXT,
    import_path   TEXT,
    imported_title_id INTEGER,
    audited_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_import ON findings(import_path);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT,
    totp_secret    TEXT,
    totp_pending   TEXT,
    last_totp_step INTEGER,
    mode           TEXT NOT NULL DEFAULT 'password',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
"""

ADDED_COLUMNS = (
    ("titles", "candidates_json", "TEXT"),
    ("titles", "pinned_json", "TEXT"),
    ("titles", "episode_last", "INTEGER"),
    ("titles", "supersedes_json", "TEXT"),
    ("titles", "sibling_compare_json", "TEXT"),
    ("titles", "queue_order", "INTEGER"),
    ("findings", "duration_s", "REAL"),
)

JSON_COLUMNS = {
    "probe": "probe_json",
    "identity": "identity_json",
    "decision": "decision_json",
    "comparison": "compare_json",
    "output_probe": "output_probe_json",
    "reasons": "reasons_json",
    "candidates": "candidates_json",
    "pinned": "pinned_json",
    "supersedes": "supersedes_json",
    "sibling_comparison": "sibling_compare_json",
}


#----- JSON column helpers
def _json(value):
    if value is None:
        return None
    return json.dumps(value, default=str)


def _unjson(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def normalise_reasons(reasons):
    if reasons is None:
        return []
    if isinstance(reasons, (str, bytes)):
        reasons = [reasons]
    out = []
    for item in reasons:
        if isinstance(item, dict):
            out.append({"stage": item.get("stage"), "text": str(item.get("text") or "")})
        else:
            out.append({"stage": None, "text": str(item)})
    return [e for e in out if e["text"]]


#----- The store
class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._add_missing_columns()
            self._db.commit()

    def _add_missing_columns(self):
        for table, column, declaration in ADDED_COLUMNS:
            present = {r["name"] for r in self._db.execute("PRAGMA table_info(%s)" % table)}
            if column not in present:
                self._db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))
                log.info("added column %s.%s", table, column)

    def close(self):
        with self._lock:
            self._db.close()

    def _row_to_dict(self, row):
        if row is None:
            return None
        d = dict(row)
        d["probe"] = _unjson(d.pop("probe_json", None))
        d["identity"] = _unjson(d.pop("identity_json", None))
        d["decision"] = _unjson(d.pop("decision_json", None))
        d["comparison"] = _unjson(d.pop("compare_json", None))
        d["output_probe"] = _unjson(d.pop("output_probe_json", None))
        d["reasons"] = _unjson(d.pop("reasons_json", None)) or []
        d["candidates"] = _unjson(d.pop("candidates_json", None))
        d["pinned"] = _unjson(d.pop("pinned_json", None))
        d["supersedes"] = _unjson(d.pop("supersedes_json", None)) or []
        d["sibling_comparison"] = _unjson(d.pop("sibling_compare_json", None))
        d["overridden"] = bool(d.get("overridden"))
        return d

    def upsert_source(self, source_path, kind=None):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "SELECT id FROM titles WHERE source_path = ?", (str(source_path),)
            )
            row = cur.fetchone()
            if row:
                return row["id"]
            cur = self._db.execute(
                "INSERT INTO titles (source_path, kind, stage, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(source_path), kind, DETECTED, now, now),
            )
            self._db.commit()
            title_id = cur.lastrowid
        self.record(title_id, DETECTED, "detected in import")
        self.place(title_id)
        return title_id

    #----- Queries
    def get(self, title_id):
        with self._lock:
            cur = self._db.execute("SELECT * FROM titles WHERE id = ?", (title_id,))
            return self._row_to_dict(cur.fetchone())

    def by_output_path(self, output_path):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE output_path = ?", (str(output_path),)
            )
            return self._row_to_dict(cur.fetchone())

    def sibling_in_flight(self, row):
        #----- two-sided:  a lower id, or a higher id already past COMPARED, so one of a concurrent pair holds.
        kind = row.get("kind")
        excluded = COMPLETE + (QUARANTINED,)
        placeholders = ",".join("?" for _ in excluded)
        past_compared = PIPELINE[PIPELINE.index(COMPARED) + 1:]
        past_placeholders = ",".join("?" for _ in past_compared)
        where = "kind = ? AND id != ? AND stage NOT IN (%s) AND (id < ? OR stage IN (%s))" % (
            placeholders, past_placeholders)
        params = [kind, row["id"]] + list(excluded) + [row["id"]] + list(past_compared)
        if kind == "movie":
            if row.get("tmdb"):
                where += " AND tmdb = ?"
                params.append(str(row["tmdb"]))
            elif row.get("imdb"):
                where += " AND imdb = ?"
                params.append(str(row["imdb"]))
            else:
                return None
        else:
            if not (row.get("tvdb") and row.get("season") is not None and row.get("episode") is not None):
                return None
            first = int(row["episode"])
            last = int(row.get("episode_last") or first)
            where += (" AND tvdb = ? AND season = ? AND episode IS NOT NULL"
                      " AND episode <= ? AND COALESCE(episode_last, episode) >= ?")
            params += [str(row["tvdb"]), int(row["season"]), last, first]
        with self._lock:
            cur = self._db.execute("SELECT * FROM titles WHERE %s ORDER BY id LIMIT 1" % where, params)
            return self._row_to_dict(cur.fetchone())

    def by_source(self, source_path):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE source_path = ?", (str(source_path),)
            )
            return self._row_to_dict(cur.fetchone())

    def all(self, stage=None, limit=500):
        query = "SELECT * FROM titles"
        params = []
        if stage:
            query += " WHERE stage = ?"
            params.append(stage)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cur = self._db.execute(query, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def active(self):
        placeholders = ",".join("?" for _ in STOPPED + TERMINAL)
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE stage NOT IN (%s) ORDER BY created_at" % placeholders,
                STOPPED + TERMINAL,
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def held(self):
        return self.all(stage=HELD)

    #----- The queue
    def queue_rows(self):
        #----- everything neither stopped nor complete;  a PUBLISHED row is never picked up again.
        excluded = STOPPED + COMPLETE
        placeholders = ",".join("?" for _ in excluded)
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE stage NOT IN (%s)"
                " ORDER BY queue_order IS NULL, queue_order, id" % placeholders,
                excluded,
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def place(self, title_id):
        #----- the back of the queue, or the end of the show's block so a show stays contiguous.
        row = self.get(title_id)
        if row is None:
            return
        others = [r for r in self.queue_rows() if r["id"] != title_id]
        ordered = [r["id"] for r in others]
        index = len(ordered)
        if row.get("kind") == "tv" and row.get("show"):
            for i, r in enumerate(others):
                if r.get("kind") == "tv" and r.get("show") == row["show"]:
                    index = i + 1
        ordered.insert(index, title_id)
        self.renumber(ordered)

    def renumber(self, ordered_ids):
        with self._lock:
            self._db.executemany(
                "UPDATE titles SET queue_order = ? WHERE id = ?",
                [(n, i) for n, i in enumerate(ordered_ids, 1)],
            )
            self._db.commit()

    def needs_decision(self):
        rows = self.all(stage=HELD) + self.all(stage=FAILED)
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)

    def poster_urls(self):
        with self._lock:
            cur = self._db.execute(
                "SELECT DISTINCT poster_url FROM titles WHERE poster_url IS NOT NULL"
            )
            return [r["poster_url"] for r in cur.fetchall()]

    def counts_by_stage(self):
        with self._lock:
            cur = self._db.execute("SELECT stage, COUNT(*) n FROM titles GROUP BY stage")
            return {r["stage"]: r["n"] for r in cur.fetchall()}

    #----- Mutation and stage transitions
    def update(self, title_id, **fields):
        log.debug("title %s fields updated: %s", title_id, ", ".join(sorted(fields)))
        if not fields:
            return
        for key, column in JSON_COLUMNS.items():
            if key in fields:
                fields[column] = _json(fields.pop(key))
        fields["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % k for k in fields)
        params = list(fields.values()) + [title_id]
        with self._lock:
            self._db.execute("UPDATE titles SET %s WHERE id = ?" % assignments, params)
            self._db.commit()

    def advance(self, title_id, stage, detail=None, **fields):
        log.debug("title %s -> %s%s", title_id, stage, ": " + detail if detail else "")
        fields["stage"] = stage
        self.update(title_id, **fields)
        self.record(title_id, stage, detail)

    def hold(self, title_id, reasons):
        entries = normalise_reasons(reasons)
        summary = "; ".join(e["text"] for e in entries)
        self.advance(title_id, HELD, summary, reason=summary, reasons=entries)

    def hold_for_retry(self, title_id, reason, delay):
        row = self.get(title_id) or {}
        attempts = (row.get("attempts") or 0) + 1
        self.advance(
            title_id,
            HELD,
            "%s (attempt %d, retrying in %ds)" % (reason, attempts, int(delay)),
            reason=reason,
            reasons=normalise_reasons(reason),
            attempts=attempts,
            retry_after=time.time() + delay,
        )
        return attempts

    def due_for_retry(self, max_attempts):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE stage = ? AND retry_after IS NOT NULL"
                " AND retry_after <= ? AND attempts < ?",
                (HELD, now, max_attempts),
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

    #----- History
    def record(self, title_id, stage, detail=None):
        with self._lock:
            self._db.execute(
                "INSERT INTO history (title_id, stage, detail, at) VALUES (?, ?, ?, ?)",
                (title_id, stage, detail, time.time()),
            )
            self._db.commit()

    def reset_for_reimport(self, title_id):
        self.advance(
            title_id,
            DETECTED,
            "source reappeared in import, reset for a fresh run",
            kind=None,
            title=None,
            year=None,
            show=None,
            season=None,
            episode=None,
            episode_last=None,
            tmdb=None,
            imdb=None,
            tvdb=None,
            job_id=None,
            work_path=None,
            output_path=None,
            encoder=None,
            grain_ratio=None,
            probe=None,
            identity=None,
            decision=None,
            comparison=None,
            output_probe=None,
            reason=None,
            reasons=None,
            candidates=None,
            pinned=None,
            attempts=0,
            retry_after=None,
            overridden=0,
            quarantine_path=None,
            origin_path=None,
            poster_url=None,
            supersedes=None,
            sibling_comparison=None,
        )
        self.place(title_id)

    def forget(self, title_id):
        with self._lock:
            self._db.execute("DELETE FROM history WHERE title_id = ?", (title_id,))
            self._db.execute("DELETE FROM titles WHERE id = ?", (title_id,))
            self._db.commit()
        log.debug("title %s removed from the store", title_id)

    def history(self, title_id, limit=200):
        with self._lock:
            cur = self._db.execute(
                "SELECT stage, detail, at FROM history WHERE title_id = ?"
                " ORDER BY at ASC LIMIT ?",
                (title_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def resumable(self):
        rows = self.active()
        return [r for r in rows if r["stage"] not in STOPPED]

    #----- Library audit
    def _finding_to_dict(self, row):
        if row is None:
            return None
        d = dict(row)
        d["checks"] = _unjson(d.pop("checks_json", None)) or []
        d["measured"] = _unjson(d.pop("measured_json", None)) or {}
        return d

    def audit_seen(self, path):
        with self._lock:
            cur = self._db.execute(
                "SELECT size, mtime FROM findings WHERE path = ?", (str(path),)
            )
            row = cur.fetchone()
            return (row["size"], row["mtime"]) if row else None

    def audit_record(self, path, kind, size, mtime, checks, measured, summary, duration_s=None):
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO findings (path, kind, size, mtime, checks_json, measured_json,"
                " summary, duration_s, audited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, size=excluded.size,"
                " mtime=excluded.mtime, checks_json=excluded.checks_json,"
                " measured_json=excluded.measured_json, summary=excluded.summary,"
                " duration_s=excluded.duration_s, audited_at=excluded.audited_at",
                (str(path), kind, size, mtime, _json(checks), _json(measured), summary, duration_s, now),
            )
            self._db.commit()

    def audit_update(self, path, checks, measured, summary, duration_s=None):
        with self._lock:
            if duration_s is None:
                self._db.execute(
                    "UPDATE findings SET checks_json = ?, measured_json = ?, summary = ? WHERE path = ?",
                    (_json(checks), _json(measured), summary, str(path)),
                )
            else:
                self._db.execute(
                    "UPDATE findings SET checks_json = ?, measured_json = ?, summary = ?, duration_s = ?"
                    " WHERE path = ?",
                    (_json(checks), _json(measured), summary, duration_s, str(path)),
                )
            self._db.commit()

    def audit_rows(self, kind):
        with self._lock:
            cur = self._db.execute("SELECT * FROM findings WHERE kind = ? ORDER BY path", (kind,))
            return [self._finding_to_dict(r) for r in cur.fetchall()]

    def audit_forget_all(self):
        with self._lock:
            cur = self._db.execute("DELETE FROM findings")
            self._db.commit()
            return cur.rowcount

    def audit_forget_missing(self, present):
        present = set(str(p) for p in present)
        with self._lock:
            cur = self._db.execute("SELECT id, path FROM findings")
            gone = [r["id"] for r in cur.fetchall() if r["path"] not in present]
            for finding_id in gone:
                self._db.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
            self._db.commit()
        return len(gone)

    def finding(self, finding_id):
        with self._lock:
            cur = self._db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
            return self._finding_to_dict(cur.fetchone())

    def finding_by_import_path(self, import_path):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM findings WHERE import_path = ?", (str(import_path),)
            )
            return self._finding_to_dict(cur.fetchone())

    def findings(self):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM findings WHERE checks_json IS NOT NULL AND checks_json != '[]'"
                " ORDER BY path"
            )
            return [self._finding_to_dict(r) for r in cur.fetchall()]

    def audit_totals(self):
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) n, SUM(CASE WHEN checks_json IS NOT NULL AND checks_json != '[]'"
                " THEN 1 ELSE 0 END) f, MAX(audited_at) latest FROM findings"
            )
            row = cur.fetchone()
            return {"audited": row["n"] or 0, "findings": row["f"] or 0, "latest": row["latest"]}

    def finding_mark_import(self, finding_id, import_path):
        with self._lock:
            self._db.execute(
                "UPDATE findings SET import_path = ?, imported_title_id = NULL WHERE id = ?",
                (str(import_path), finding_id),
            )
            self._db.commit()

    def link_import(self, title_id, source_path):
        finding = self.finding_by_import_path(source_path)
        if finding is None:
            return None
        with self._lock:
            self._db.execute(
                "UPDATE findings SET imported_title_id = ? WHERE id = ?", (title_id, finding["id"])
            )
            self._db.commit()
        self.update(title_id, origin_path=finding["path"])
        log.info(
            "title %s was imported from the library for repair, origin %s",
            title_id, finding["path"],
        )
        return finding["path"]

    #----- Settings
    def settings_all(self):
        with self._lock:
            cur = self._db.execute("SELECT key, value_json FROM settings ORDER BY key")
            return [(r["key"], r["value_json"]) for r in cur.fetchall()]

    def settings_put(self, rows):
        with self._lock:
            self._db.executemany(
                "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                [(str(k), str(v), float(at)) for k, v, at in rows],
            )
            self._db.commit()

    def settings_delete(self, keys):
        with self._lock:
            self._db.executemany("DELETE FROM settings WHERE key = ?", [(str(k),) for k in keys])
            self._db.commit()

    #----- Users
    def user_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

    def user_get(self, user_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def user_by_name(self, username):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (str(username),)
            ).fetchone()
            return dict(row) if row else None

    def users(self):
        with self._lock:
            cur = self._db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")
            return [dict(r) for r in cur.fetchall()]

    def user_create(self, username, password_hash, mode, totp_secret=None, only_if_empty=False):
        now = time.time()
        with self._lock:
            #----- the emptiness check and the insert share the lock, so the first account is created once.
            if only_if_empty and self._db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]:
                return None
            try:
                cur = self._db.execute(
                    "INSERT INTO users (username, password_hash, totp_secret, mode, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (str(username), password_hash, totp_secret, mode, now, now),
                )
            except sqlite3.IntegrityError:
                return None
            self._db.commit()
            return cur.lastrowid

    def user_update(self, user_id, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % k for k in fields)
        with self._lock:
            self._db.execute(
                "UPDATE users SET %s WHERE id = ?" % assignments, list(fields.values()) + [user_id]
            )
            self._db.commit()

    def user_delete(self, user_id):
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._db.commit()

    #----- Sessions, stored by the hash of the token and never the token
    def session_create(self, token_hash, user_id, created_at, expires_at):
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, created_at, expires_at),
            )
            self._db.commit()

    def session_get(self, token_hash):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            return dict(row) if row else None

    def session_delete(self, token_hash):
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            self._db.commit()

    def sessions_delete_for_user(self, user_id):
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.commit()

    def sessions_purge_expired(self, now):
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            self._db.commit()
