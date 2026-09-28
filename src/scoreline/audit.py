"""Append-only, hash-chained audit trail.

Every row stores sha256(prev_hash + canonical payload). Rows cannot be updated
or deleted (triggers in schema.sql). If someone edits the file directly, the
chain stops verifying and the audit page says exactly where.
"""

from __future__ import annotations

import hashlib
import json

from .db import all_, now, one

GENESIS = "0" * 64


def _digest(prev_hash: str, at: str, event_id, actor_id, action, subject, summary, detail) -> str:
    payload = json.dumps(
        [at, event_id, actor_id, action, subject, summary, detail],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256((prev_hash + payload).encode()).hexdigest()


def record(conn, *, actor, action: str, summary: str, event_id=None, subject: str = "", detail=None):
    """Write one audit row. `actor` is a user row/dict or None for the system.

    Call inside the same transaction as the change it describes, so a change
    and its audit entry either both land or neither does.
    """
    actor_id = actor["id"] if actor else None
    actor_name = actor["name"] if actor else "System"
    detail_json = json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)
    last = one(conn, "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1")
    prev_hash = last["hash"] if last else GENESIS
    at = now()
    digest = _digest(prev_hash, at, event_id, actor_id, action, subject, summary, detail_json)
    conn.execute(
        "INSERT INTO audit_log(at, event_id, actor_id, actor_name, action, subject, summary, detail, prev_hash, hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (at, event_id, actor_id, actor_name, action, subject, summary, detail_json, prev_hash, digest),
    )


def verify_chain(conn) -> dict:
    """Walk the whole chain. Returns {'ok': bool, 'count': n, 'broken_at': seq|None}."""
    prev = GENESIS
    count = 0
    for row in all_(conn, "SELECT * FROM audit_log ORDER BY seq"):
        expected = _digest(prev, row["at"], row["event_id"], row["actor_id"], row["action"],
                           row["subject"], row["summary"], row["detail"])
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"ok": False, "count": count, "broken_at": row["seq"]}
        prev = row["hash"]
        count += 1
    return {"ok": True, "count": count, "broken_at": None}
