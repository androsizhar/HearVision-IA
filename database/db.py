"""
database/db.py
---------------
Local SQLite persistence layer for a single-user automation tool.

Stores the minimum needed for the record -> analyze -> ask -> execute
cycle to work and improve over time:

  - plans: the learned workflow for each portal, so it doesn't need to be
    re-explained on every run
  - field_mappings: confidence score for each mapped field, refined with use
  - sessions: a simple log of what was executed and the outcome
  - secrets: an encrypted vault so credentials are never stored in plain
    text in session/plan.json (see postprocessing/crypto.py)

There is no user or login-session table: this runs locally for a single
person/project, not as a multi-user service.
"""

import sqlite3
import json
import os
from typing import Optional
import secrets
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "hearvision.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(cur, table: str, column: str, ddl: str):
    """SQLite has no 'ADD COLUMN IF NOT EXISTS', so check manually. Used to
    migrate databases created before a given column existed."""
    cols = [r["name"] for r in cur.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS plans (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        portal_url          TEXT NOT NULL,
        source_platform     TEXT,
        target_platform     TEXT,
        goal                TEXT,
        plan_json           TEXT NOT NULL,
        active              INTEGER DEFAULT 1,
        created_at          TEXT NOT NULL,
        updated_at          TEXT
    );

    CREATE TABLE IF NOT EXISTS field_mappings (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        portal_url          TEXT NOT NULL,
        source_field        TEXT NOT NULL,
        target_field        TEXT NOT NULL,
        initial_confidence  REAL NOT NULL,
        current_confidence  REAL NOT NULL,
        confirmations       INTEGER DEFAULT 0,
        corrections         INTEGER DEFAULT 0,
        created_at          TEXT NOT NULL,
        updated_at          TEXT
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at          TEXT NOT NULL,
        name                TEXT,
        user_email          TEXT,
        portal_url          TEXT,
        source_platform     TEXT,
        target_platform     TEXT,
        step_count          INTEGER DEFAULT 0,
        successful_steps    INTEGER DEFAULT 0,
        failed_steps        INTEGER DEFAULT 0,
        warning_steps       INTEGER DEFAULT 0,
        duration_sec        REAL,
        plan_id             INTEGER,
        result_json         TEXT,
        FOREIGN KEY (plan_id) REFERENCES plans(id)
    );

    CREATE TABLE IF NOT EXISTS errors (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL,
        step        INTEGER,
        action      TEXT,
        description TEXT,
        created_at  TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    -- Secrets vault: credentials collected during Phase B are never stored
    -- in plain text in the plan (neither in sessions/plan.json nor in the
    -- 'plans' table) -- only a secret_id pointing here, encrypted with
    -- Fernet (postprocessing/crypto.py) and with a short expiration.
    CREATE TABLE IF NOT EXISTS secrets (
        id              TEXT PRIMARY KEY,
        encrypted_value TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    );
    """)
    conn.commit()

    # Lightweight migration for databases created before this column existed.
    _add_column_if_missing(cur, "sessions", "name", "name TEXT")
    conn.commit()
    conn.close()


# --- Plans -------------------------------------------------------------------

def save_plan(plan: dict) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute("UPDATE plans SET active=0 WHERE portal_url=?", (plan.get("portal_url", ""),))
    cur.execute("""
        INSERT INTO plans (portal_url, source_platform, target_platform,
                           goal, plan_json, active, created_at)
        VALUES (?,?,?,?,?,1,?)
    """, (plan.get("portal_url", ""), plan.get("source_platform", ""),
          plan.get("target_platform", ""), plan.get("goal", ""),
          json.dumps(plan, ensure_ascii=False), now))
    plan_id = cur.lastrowid
    for field in plan.get("field_mappings", []):
        confidence = field.get("confidence", 1.0)
        cur.execute("""
            INSERT INTO field_mappings (portal_url, source_field, target_field,
                                        initial_confidence, current_confidence, created_at)
            VALUES (?,?,?,?,?,?)
        """, (plan.get("portal_url", ""), field.get("source_field", ""),
              field.get("target_field", ""), confidence, confidence, now))
    conn.commit()
    conn.close()
    return plan_id


def load_active_plan(portal_url: str) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT plan_json FROM plans WHERE portal_url=? AND active=1 ORDER BY id DESC LIMIT 1",
                (portal_url,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["plan_json"]) if row else None


# --- Sessions (execution history) --------------------------------------------

def save_session(plan: dict, results: list, email: str,
                 duration_sec: float = None, plan_id: int = None,
                 name: str = None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    ok = sum(1 for r in results if r.get("status") == "ok")
    err = sum(1 for r in results if r.get("status") == "error")
    warn = sum(1 for r in results if r.get("status") == "warning")
    if not name:
        # Human-readable default name, editable later from the History view.
        name = f"{plan.get('source_platform', '?')} -> {plan.get('target_platform', '?')}"
    cur.execute("""
        INSERT INTO sessions (created_at, name, user_email, portal_url,
                              source_platform, target_platform,
                              step_count, successful_steps, failed_steps, warning_steps,
                              duration_sec, plan_id, result_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, name, email, plan.get("portal_url", ""), plan.get("source_platform", ""),
          plan.get("target_platform", ""),
          len(results), ok, err, warn,
          duration_sec, plan_id,
          json.dumps(results, ensure_ascii=False)))
    session_id = cur.lastrowid
    for r in results:
        if r.get("status") != "ok":
            cur.execute("""
                INSERT INTO errors (session_id, step, action, description, created_at)
                VALUES (?,?,?,?,?)
            """, (session_id, r.get("step"), r.get("action", ""),
                  str(r.get("extracted_data", "")), now))
    conn.commit()
    conn.close()
    return session_id


def get_history(limit: int = 50) -> list:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, created_at, name, user_email, portal_url, source_platform,
               target_platform, step_count, successful_steps, failed_steps,
               duration_sec, plan_id
        FROM sessions ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_session_by_id(session_id: int) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("result_json"):
        d["results"] = json.loads(d.pop("result_json"))
    return d


def rename_session(session_id: int, name: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE sessions SET name=? WHERE id=?", (name.strip()[:120], session_id))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def delete_session(session_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM errors WHERE session_id=?", (session_id,))
    cur.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def get_plan_by_id(plan_id: int) -> Optional[dict]:
    """Unlike load_active_plan() (which only returns the ACTIVE plan for a
    portal), this fetches the exact plan used in a specific past session --
    needed to re-run a history entry even if a newer plan version now
    exists for that same portal."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT plan_json FROM plans WHERE id=?", (plan_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["plan_json"]) if row else None


def get_statistics() -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM sessions")
    total = cur.fetchone()["total"]
    cur.execute("SELECT AVG(CAST(successful_steps AS REAL)/NULLIF(step_count,0))*100 as rate FROM sessions")
    rate = cur.fetchone()["rate"] or 0
    cur.execute("SELECT COUNT(*) as total FROM plans WHERE active=1")
    active_plans = cur.fetchone()["total"]
    cur.execute("""
        SELECT action, COUNT(*) as total FROM errors
        GROUP BY action ORDER BY total DESC LIMIT 10
    """)
    errors_by_action = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {
        "total_sessions": total,
        "success_rate_pct": round(rate, 1),
        "active_plans": active_plans,
        "errors_by_action": errors_by_action,
    }


# --- Secrets vault (Phase B credentials) -------------------------------------
# Fail-closed: if no encryption key is configured, save_secret() raises
# instead of storing the credential in plain text (see postprocessing/crypto.py).

DEFAULT_SECRET_TTL_HOURS = 48


def save_secret(value: str, ttl_hours: int = DEFAULT_SECRET_TTL_HOURS) -> str:
    from postprocessing.crypto import encrypt_text, EncryptionUnavailableError
    try:
        encrypted = encrypt_text(value, strict=True)
    except EncryptionUnavailableError:
        raise RuntimeError(
            "Cannot save credential: HEARVISION_ENC_KEY is not configured. "
            "Secrets are never stored unencrypted."
        )
    secret_id = secrets.token_urlsafe(24)
    now = datetime.now()
    conn = get_connection()
    conn.execute(
        "INSERT INTO secrets (id, encrypted_value, created_at, expires_at) VALUES (?,?,?,?)",
        (secret_id, encrypted, now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat()),
    )
    conn.commit()
    conn.close()
    return secret_id


def read_secret(secret_id: str) -> Optional[str]:
    from postprocessing.crypto import decrypt_text
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT encrypted_value, expires_at FROM secrets WHERE id=?", (secret_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        delete_secret(secret_id)
        return None
    return decrypt_text(row["encrypted_value"])


def delete_secret(secret_id: str) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM secrets WHERE id=?", (secret_id,))
    conn.commit()
    conn.close()


def clean_expired_secrets() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM secrets WHERE expires_at < ?", (datetime.now().isoformat(),))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


init_db()
