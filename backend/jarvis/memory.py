"""Persistent memory: conversations, messages, device profiles, and semantic recall.

Uses SQLite for durable storage. Semantic recall uses cosine similarity over embeddings
when an embedding endpoint is configured; otherwise it falls back to recency + keyword match.
"""

from __future__ import annotations

import json
import math
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

    # --- Semantic memory ----------------------------------------------
    def add_memory(self, kind: str, text: str, embedding: list[float] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO memories (kind, text, embedding, created_at) VALUES (?, ?, ?, ?)",
            (kind, text, json.dumps(embedding) if embedding else None, time.time()),
        )
        self.conn.commit()

    def list_memories(
        self, kinds: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            rows = self.conn.execute(
                f"SELECT id, kind, text, created_at FROM memories WHERE kind IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, kind, text, created_at FROM memories ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recall(
        self, query: str, query_embedding: list[float] | None = None, limit: int = 6
    ) -> list[str]:
        # Prefer long-term consolidated memories, then everything else.
        rows = self.conn.execute(
            "SELECT text, embedding, kind FROM memories ORDER BY "
            "CASE kind WHEN 'long_term' THEN 0 WHEN 'rem_theme' THEN 1 ELSE 2 END, id DESC"
        ).fetchall()
        if not rows:
            return []

        if query_embedding:
            scored: list[tuple[float, str]] = []
            for r in rows:
                if not r["embedding"]:
                    continue
                emb = json.loads(r["embedding"])
                boost = 0.08 if r["kind"] == "long_term" else 0.0
                scored.append((_cosine(query_embedding, emb) + boost, r["text"]))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                return [t for _, t in scored[:limit]]

        terms = {w.lower() for w in query.split() if len(w) > 3}
        scored_kw: list[tuple[int, str]] = []
        for r in rows:
            text = r["text"]
            overlap = sum(1 for t in terms if t in text.lower())
            if r["kind"] == "long_term":
                overlap += 1
            scored_kw.append((overlap, text))
        scored_kw.sort(key=lambda x: x[0], reverse=True)
        top = [t for score, t in scored_kw if score > 0][:limit]
        if top:
            return top
        return [r["text"] for r in rows[:limit]]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)
