"""Persistent memory: conversations, messages, device profiles, and semantic recall.

Uses SQLite for durable storage. Semantic recall uses cosine similarity over embeddings
when an embedding endpoint is configured; otherwise it falls back to recency + keyword match.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR

DB_PATH = DATA_DIR / "jarvis.db"


@dataclass
class Conversation:
    id: int
    title: str
    created_at: float
    updated_at: float


class Memory:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                attachments TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                profile TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS peripherals (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT,
                connected INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 1,
                profile TEXT NOT NULL,
                facts TEXT NOT NULL DEFAULT '[]',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_items (
                id TEXT PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                conversation_id INTEGER,
                result TEXT,
                log TEXT,
                created_at REAL NOT NULL,
                finished_at REAL
            );
            """
        )
        # Older DBs may lack the attachments column.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "attachments" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
        self.conn.commit()

    # --- Conversations -------------------------------------------------
    def create_conversation(self, title: str = "New conversation") -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_conversations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_conversation(self, conversation_id: int, title: str) -> None:
        self.conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), conversation_id),
        )
        self.conn.commit()

    def delete_conversation(self, conversation_id: int) -> None:
        self.conn.execute("DELETE FROM activity_items WHERE conversation_id = ?", (conversation_id,))
        self.conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        self.conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        self.conn.commit()

    # --- Activity monitor ----------------------------------------------
    def replace_activity(self, conversation_id: int, items: list[dict[str, Any]]) -> None:
        """Persist the full activity feed for a conversation (replace semantics)."""
        self.conn.execute("DELETE FROM activity_items WHERE conversation_id = ?", (conversation_id,))
        now = time.time()
        for idx, item in enumerate(items[-500:]):
            iid = str(item.get("id") or f"a{now}-{idx}")
            self.conn.execute(
                "INSERT OR REPLACE INTO activity_items (id, conversation_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (iid, conversation_id, json.dumps(item), now + idx * 0.001),
            )
        self.conn.commit()

    def get_activity(self, conversation_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM activity_items WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except json.JSONDecodeError:
                continue
        return out

    def clear_activity(self, conversation_id: int) -> None:
        self.conn.execute("DELETE FROM activity_items WHERE conversation_id = ?", (conversation_id,))
        self.conn.commit()

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        now = time.time()
        payload = json.dumps(attachments) if attachments else None
        self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, attachments) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, payload),
        )
        self.conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        self.conn.commit()

    def get_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, content, created_at, attachments FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            raw = d.pop("attachments", None)
            if raw:
                try:
                    d["attachments"] = json.loads(raw)
                except json.JSONDecodeError:
                    d["attachments"] = []
            else:
                d["attachments"] = []
            result.append(d)
        return result

    # --- Background tasks ------------------------------------------------
    TASK_LOG_CAP = 200

    def create_task(
        self,
        task_id: str,
        kind: str,
        title: str,
        goal: str,
        conversation_id: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, kind, title, goal, status, conversation_id, result, log, created_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, NULL, '[]', ?)",
            (task_id, kind, title, goal, conversation_id, time.time()),
        )
        self.conn.commit()

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: str | None = None,
        finished: bool = False,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if result is not None:
            sets.append("result = ?")
            params.append(result)
        if finished:
            sets.append("finished_at = ?")
            params.append(time.time())
        if not sets:
            return
        params.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def append_task_log(self, task_id: str, entries: list[dict[str, Any]]) -> None:
        row = self.conn.execute("SELECT log FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return
        try:
            log = json.loads(row["log"] or "[]")
        except json.JSONDecodeError:
            log = []
        log.extend(entries)
        log = log[-self.TASK_LOG_CAP:]
        self.conn.execute("UPDATE tasks SET log = ? WHERE id = ?", (json.dumps(log), task_id))
        self.conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_row_to_dict(row) if row else None

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._task_row_to_dict(r) for r in rows]

    def delete_task(self, task_id: str) -> None:
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    def fail_orphaned_tasks(self, reason: str = "Server restarted.") -> int:
        cur = self.conn.execute(
            "UPDATE tasks SET status = 'failed', result = ?, finished_at = ? WHERE status = 'running'",
            (reason, time.time()),
        )
        self.conn.commit()
        return cur.rowcount

    @staticmethod
    def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["log"] = json.loads(d.get("log") or "[]")
        except json.JSONDecodeError:
            d["log"] = []
        return d

    # --- Devices -------------------------------------------------------
    def upsert_device(self, fingerprint: str, name: str, profile: dict[str, Any]) -> None:
        now = time.time()
        existing = self.conn.execute(
            "SELECT id FROM devices WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        payload = json.dumps(profile)
        if existing:
            self.conn.execute(
                "UPDATE devices SET name = ?, profile = ?, updated_at = ? WHERE fingerprint = ?",
                (name, payload, now, fingerprint),
            )
        else:
            self.conn.execute(
                "INSERT INTO devices (fingerprint, name, profile, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (fingerprint, name, payload, now, now),
            )
        self.conn.commit()

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM devices ORDER BY updated_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["profile"] = json.loads(d["profile"])
            except json.JSONDecodeError:
                pass
            result.append(d)
        return result

    # --- Peripherals ---------------------------------------------------
    def upsert_peripheral(self, peripheral: dict[str, Any]) -> None:
        now = time.time()
        pid = str(peripheral.get("id") or "").strip()
        if not pid:
            return
        payload = json.dumps(peripheral)
        facts = json.dumps(peripheral.get("facts") or [])
        first_seen = float(peripheral.get("first_seen") or now)
        last_seen = float(peripheral.get("last_seen") or now)
        existing = self.conn.execute(
            "SELECT first_seen FROM peripherals WHERE id = ?", (pid,)
        ).fetchone()
        if existing:
            first_seen = float(existing["first_seen"] or first_seen)
            self.conn.execute(
                "UPDATE peripherals SET kind=?, name=?, address=?, connected=?, available=?, "
                "profile=?, facts=?, last_seen=? WHERE id=?",
                (
                    peripheral.get("kind") or "unknown",
                    peripheral.get("name") or pid,
                    peripheral.get("address") or "",
                    1 if peripheral.get("connected") else 0,
                    1 if peripheral.get("available", True) else 0,
                    payload,
                    facts,
                    last_seen,
                    pid,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO peripherals (id, kind, name, address, connected, available, "
                "profile, facts, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    peripheral.get("kind") or "unknown",
                    peripheral.get("name") or pid,
                    peripheral.get("address") or "",
                    1 if peripheral.get("connected") else 0,
                    1 if peripheral.get("available", True) else 0,
                    payload,
                    facts,
                    first_seen,
                    last_seen,
                ),
            )
        self.conn.commit()

    def get_peripheral(self, peripheral_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM peripherals WHERE id = ?", (peripheral_id,)
        ).fetchone()
        return self._peripheral_row(row) if row else None

    def list_peripherals(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM peripherals WHERE kind = ? ORDER BY connected DESC, last_seen DESC",
                (kind,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM peripherals ORDER BY connected DESC, kind, last_seen DESC"
            ).fetchall()
        return [self._peripheral_row(r) for r in rows]

    def delete_peripheral(self, peripheral_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM peripherals WHERE id = ?", (peripheral_id,))
        self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _peripheral_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        profile: dict[str, Any] = {}
        try:
            profile = json.loads(d.pop("profile", None) or "{}")
        except json.JSONDecodeError:
            profile = {}
        if isinstance(profile, dict):
            merged = {**profile}
        else:
            merged = {}
        merged["id"] = d.get("id")
        merged["kind"] = d.get("kind") or merged.get("kind")
        merged["name"] = d.get("name") or merged.get("name")
        merged["address"] = d.get("address") or merged.get("address") or ""
        merged["connected"] = bool(d.get("connected"))
        merged["available"] = bool(d.get("available"))
        merged["first_seen"] = d.get("first_seen")
        merged["last_seen"] = d.get("last_seen")
        try:
            facts = json.loads(d.get("facts") or "[]")
        except json.JSONDecodeError:
            facts = merged.get("facts") or []
        merged["facts"] = facts if isinstance(facts, list) else []
        return merged

    # --- Semantic memory ----------------------------------------------
    def has_memory(self, kind: str, text: str) -> bool:
        """Is this exact note already stored? Used to keep periodic re-scans from
        duplicating rows (and from paying for a redundant embedding)."""
        row = self.conn.execute(
            "SELECT 1 FROM memories WHERE kind = ? AND text = ? LIMIT 1",
            (kind, text),
        ).fetchone()
        return row is not None

    def add_memory(self, kind: str, text: str, embedding: list[float] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO memories (kind, text, embedding, created_at) VALUES (?, ?, ?, ?)",
            (kind, text, json.dumps(embedding) if embedding else None, time.time()),
        )
        self.conn.commit()

    def list_memories(
        self,
        kinds: list[str] | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        q = (query or "").strip()
        if q:
            clauses.append("text LIKE ?")
            params.append(f"%{q}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT id, kind, text, created_at FROM memories{where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_memories(self, kinds: list[str] | None = None, query: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        q = (query or "").strip()
        if q:
            clauses.append("text LIKE ?")
            params.append(f"%{q}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM memories{where}", params).fetchone()
        return int(row["n"]) if row else 0

    def memory_stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        by_kind = {str(r["kind"]): int(r["n"]) for r in rows}
        return {"total": sum(by_kind.values()), "by_kind": by_kind}

    def delete_memory(self, memory_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_memories(self, memory_ids: list[int]) -> int:
        ids = [int(i) for i in memory_ids if int(i) > 0]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()
        return cur.rowcount

    def wipe_memories(self, kinds: list[str] | None = None) -> int:
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            cur = self.conn.execute(
                f"DELETE FROM memories WHERE kind IN ({placeholders})", kinds
            )
        else:
            cur = self.conn.execute("DELETE FROM memories")
        self.conn.commit()
        return cur.rowcount

    def recall(
        self, query: str, query_embedding: list[float] | None = None, limit: int = 6
    ) -> list[str]:
        """Return memories relevant to *query* only.

        Long-term memory is kept, but nothing is injected when relevance is weak.
        Episodic conversation notes need a higher bar than durable facts so past
        chats cannot hijack an unrelated turn.
        """
        rows = self.conn.execute(
            "SELECT text, embedding, kind FROM memories ORDER BY "
            "CASE kind WHEN 'long_term' THEN 0 WHEN 'rem_theme' THEN 1 "
            "WHEN 'device' THEN 2 WHEN 'peripheral' THEN 2 WHEN 'device_control' THEN 2 "
            "ELSE 3 END, id DESC"
        ).fetchall()
        if not rows:
            return []

        if query_embedding:
            scored: list[tuple[float, str]] = []
            seen: set[str] = set()
            for r in rows:
                if not r["embedding"]:
                    continue
                try:
                    emb = json.loads(r["embedding"])
                except json.JSONDecodeError:
                    continue
                sim = _cosine(query_embedding, emb)
                kind = (r["kind"] or "").strip()
                floor = _RECALL_COSINE_EPISODIC if kind in _EPISODIC_KINDS else _RECALL_COSINE_MIN
                if sim < floor:
                    continue
                boost = 0.05 if kind in _DURABLE_KINDS else 0.0
                text = (r["text"] or "").strip()
                if not text:
                    continue
                key = text[:160].lower()
                if key in seen:
                    continue
                seen.add(key)
                scored.append((sim + boost, text))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                return [t for _, t in scored[:limit]]

        terms = _query_terms(query)
        if not terms:
            return []

        scored_kw: list[tuple[float, str]] = []
        seen_kw: set[str] = set()
        for r in rows:
            text = (r["text"] or "").strip()
            if not text:
                continue
            low = text.lower()
            kind = (r["kind"] or "").strip()
            hits = sum(1 for t in terms if t in low)
            if hits <= 0:
                continue
            # Episodic notes need at least two distinct query terms (or one strong
            # token) so common words alone cannot drag in an old chat.
            if kind in _EPISODIC_KINDS and hits < 2 and not any(len(t) >= 6 for t in terms if t in low):
                continue
            score = float(hits)
            if kind in _DURABLE_KINDS:
                score += 0.5
            key = text[:160].lower()
            if key in seen_kw:
                continue
            seen_kw.add(key)
            scored_kw.append((score, text))
        scored_kw.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored_kw[:limit]]


# Recalled notes that are chat transcripts / REM staging — require tighter match.
_EPISODIC_KINDS = frozenset({"conversation", "staged", "short_term"})
_DURABLE_KINDS = frozenset(
    {"long_term", "device", "peripheral", "device_control", "preference", "fact"}
)
_RECALL_COSINE_MIN = 0.36
_RECALL_COSINE_EPISODIC = 0.48

_RECALL_STOPWORDS = frozenset(
    {
        "what", "with", "that", "this", "have", "from", "your", "about", "would",
        "could", "should", "there", "their", "where", "when", "which", "into",
        "than", "then", "them", "these", "those", "just", "like", "some", "more",
        "also", "only", "very", "much", "make", "made", "want", "need", "tell",
        "know", "please", "thanks", "thank", "jarvis", "asked", "answered",
        "user", "does", "doing", "done", "will", "shall", "here", "help",
        "open", "show", "give", "take", "come", "back", "again", "thing",
        "things", "something", "anything", "everything", "nothing", "other",
        "after", "before", "while", "still", "being", "been", "were", "was",
        "are", "can", "cant", "dont", "didnt", "isnt", "wasnt", "werent",
    }
)


def _query_terms(query: str) -> set[str]:
    """Content tokens from the user query — skip stopwords and tiny glue words."""
    terms: set[str] = set()
    for raw in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}", query or ""):
        w = raw.lower().strip("_-")
        if len(w) < 3 or w in _RECALL_STOPWORDS:
            continue
        # Keep short technical tokens (mcp, rgb, api, …); drop other 3-letter glue.
        if len(w) == 3 and w.isalpha() and w not in {"mcp", "rgb", "api", "gpu", "cpu", "usb", "ssd", "hdd", "vpn", "ssh", "sql", "cli", "gui", "url", "app"}:
            continue
        terms.add(w)
    return terms


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)
