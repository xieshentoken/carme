"""持久化：SQLite 存任务、消息、事件、记忆、用量。

选 SQLite 而不是 Postgres 是因为目标机器只有 8GB 内存，
单文件、零运维、备份就是拷一个文件。个人团队这个量级完全够。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    parent_id     TEXT,
    title         TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'web',
    meta          TEXT NOT NULL DEFAULT '{}',
    cost_usd      REAL NOT NULL DEFAULT 0,
    cost_known    INTEGER NOT NULL DEFAULT 1,
    tokens        INTEGER NOT NULL DEFAULT 0,
    tokens_known  INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status, created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    tool_name     TEXT NOT NULL DEFAULT '',
    step          INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, step);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL DEFAULT '',
    agent_id      TEXT NOT NULL DEFAULT '',
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS memory (
    agent_id      TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL DEFAULT '',
    agent_id           TEXT NOT NULL DEFAULT '',
    provider           TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL DEFAULT '',
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    cost_known         INTEGER NOT NULL DEFAULT 1,
    tokens_known       INTEGER NOT NULL DEFAULT 1,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_log(created_at DESC);

-- 人工确认闸门：危险动作挂在这里等用户点头
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL DEFAULT '',
    agent_id     TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    note         TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    decided_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    agent_ids TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages
    ON conversation_messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS conversation_requests (
    conversation_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (conversation_id, request_id)
);
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT PRIMARY KEY, content TEXT NOT NULL, through_seq INTEGER NOT NULL,
    model TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, mime TEXT NOT NULL,
    size INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'upload', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_conversation ON attachments(conversation_id, created_at);
"""

TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Store:
    """线程安全的 SQLite 封装，事务及共享连接的访问使用同一把锁。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
            if "conversation_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''"
                )
            for name, definition in {"cost_known": "INTEGER NOT NULL DEFAULT 1",
                                     "tokens_known": "INTEGER NOT NULL DEFAULT 1"}.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_conversation ON tasks(conversation_id, created_at)"
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(conversations)")}
            for name, definition in {"pinned_at": "REAL NOT NULL DEFAULT 0", "folder": "TEXT NOT NULL DEFAULT ''",
                                     "hidden": "INTEGER NOT NULL DEFAULT 0", "deleted_at": "REAL NOT NULL DEFAULT 0",
                                     "unread": "INTEGER NOT NULL DEFAULT 0"}.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
            self._conn.commit()
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(conversation_messages)")}
            for name, definition in {"model": "TEXT NOT NULL DEFAULT ''", "provider": "TEXT NOT NULL DEFAULT ''",
                                     "status": "TEXT NOT NULL DEFAULT 'done'"}.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE conversation_messages ADD COLUMN {name} {definition}")
            self._conn.execute("UPDATE conversation_messages SET status='interrupted' WHERE status='streaming'")
            self._conn.commit()
            usage_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(usage_log)")}
            for name, definition in {"cost_known": "INTEGER NOT NULL DEFAULT 1",
                                     "tokens_known": "INTEGER NOT NULL DEFAULT 1"}.items():
                if name not in usage_columns:
                    self._conn.execute(f"ALTER TABLE usage_log ADD COLUMN {name} {definition}")
            self._conn.commit()

    # ---------------- 底层 ----------------

    def _write(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def _query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- 任务 ----------------

    def create_task(
        self,
        agent_id: str,
        goal: str,
        *,
        title: str = "",
        parent_id: str | None = None,
        source: str = "web",
        meta: dict | None = None,
        conversation_id: str = "",
    ) -> str:
        task_id = _uid("t_")
        now = time.time()
        if parent_id and not conversation_id:
            parent = self.get_task(parent_id)
            conversation_id = (parent or {}).get("conversation_id", "")
        self._write(
            """INSERT INTO tasks (id, agent_id, parent_id, title, goal, status, source, meta, created_at, conversation_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                agent_id,
                parent_id,
                title or goal[:60],
                goal,
                "queued",
                source,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
                conversation_id,
            ),
        )
        return task_id

    def set_task_status(self, task_id: str, status: str, *, error: str = "") -> None:
        now = time.time()
        fields = ["status = ?", "error = ?"]
        params: list[Any] = [status, error]
        if status == "running":
            fields.append("started_at = ?")
            params.append(now)
        if status in TERMINAL_STATUSES:
            fields.append("finished_at = ?")
            params.append(now)
        params.append(task_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)
            if status in TERMINAL_STATUSES:
                self._conn.execute(
                    """UPDATE approvals SET status='rejected',note=?,decided_at=?
                       WHERE task_id=? AND status='pending'""",
                    (error or "任务已结束，此审批不再有效", now, task_id),
                )

    def finish_task(self, task_id: str, result: str, *, status: str = "done", error: str = "") -> None:
        now = time.time()
        self._write(
            "UPDATE tasks SET status=?, result=?, error=?, finished_at=? WHERE id=?",
            (status, result, error if status == "failed" else "", now, task_id),
        )

    def add_task_usage(self, task_id: str, cost: float, tokens: int, *,
                       cost_known: bool = True, tokens_known: bool = True) -> None:
        self._write(
            "UPDATE tasks SET cost_usd = cost_usd + ?, cost_known = cost_known AND ?, "
            "tokens = tokens + ?, tokens_known = tokens_known AND ? WHERE id = ?",
            (cost if cost_known else 0.0, int(cost_known), tokens if tokens_known else 0,
             int(tokens_known), task_id),
        )

    def get_task(self, task_id: str) -> dict | None:
        return self._query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list_tasks(
        self, *, agent_id: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self._query(sql, params)

    def children_of(self, task_id: str) -> list[dict]:
        return self._query(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at ASC", (task_id,)
        )

    def pending_tasks(self) -> list[dict]:
        return self._query(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at ASC"
        )

    def running_count(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'")
        return int(row["n"]) if row else 0

    # ---------------- 持续会话（与模型的工具 transcript 分开） ----------------

    def create_conversation(self, agent_ids: list[str], *, title: str = "") -> dict:
        members = list(dict.fromkeys(agent_ids))
        if not members or len(members) > 6 or any(not value for value in members):
            raise ValueError("会话需要 1 至 6 名成员")
        conversation_id = _uid("c_")
        now = time.time()
        self._write(
            "INSERT INTO conversations (id,title,agent_ids,kind,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (conversation_id, title, json.dumps(members),
             "group" if len(members) > 1 else "direct", now, now),
        )
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        if row:
            row["agent_ids"] = json.loads(row["agent_ids"])
        return row

    def list_conversations(self, view: str = "active") -> list[dict]:
        filters = {"active": "c.hidden=0 AND c.deleted_at=0", "hidden": "c.hidden=1 AND c.deleted_at=0",
                   "deleted": "c.deleted_at>0"}
        if view not in filters:
            raise ValueError("无效的会话列表类型")
        rows = self._query(
            f"""SELECT c.*, (SELECT content FROM conversation_messages m
               WHERE m.conversation_id=c.id ORDER BY created_at DESC, rowid DESC LIMIT 1)
               AS last_message,
               (SELECT json_group_array(DISTINCT t.agent_id) FROM tasks t
                WHERE t.conversation_id=c.id AND t.status='running') AS active_agent_ids
               FROM conversations c WHERE {filters[view]}
               ORDER BY pinned_at DESC, updated_at DESC"""
        )
        for row in rows:
            row["agent_ids"] = json.loads(row["agent_ids"])
            row["active_agent_ids"] = json.loads(row["active_agent_ids"])
        return rows

    def update_conversation(self, conversation_id: str, changes: dict) -> dict:
        if set(changes) - {"title", "pinned", "folder", "hidden", "deleted", "unread"}:
            raise ValueError("不支持的会话设置")
        with self._lock, self._conn:
            if self.get_conversation(conversation_id) is None:
                raise ValueError("会话不存在")
            if changes.get("deleted") and self._query_one(
                    "SELECT id FROM tasks WHERE conversation_id=? AND status IN ('queued','running','waiting_approval') LIMIT 1",
                    (conversation_id,)):
                raise ValueError("本对话仍有任务运行，请先停止任务再删除")
            fields = dict(changes)
            if "pinned" in fields:
                fields["pinned_at"] = time.time() if fields.pop("pinned") else 0
            if "deleted" in fields:
                fields["deleted_at"] = time.time() if fields.pop("deleted") else 0
                if not fields["deleted_at"]:
                    fields["hidden"] = False
            if fields:
                self._conn.execute(f"UPDATE conversations SET {','.join(key+'=?' for key in fields)} WHERE id=?",
                                   (*fields.values(), conversation_id))
            return self.get_conversation(conversation_id)

    def create_conversation_turn(
        self, conversation_id: str, agent_id: str, content: str, request_id: str,
        *, meta: dict | None = None, active_task_id: str | None = None, attachment_ids: list[str] | None = None,
    ) -> dict:
        """同一请求原子地保存用户消息和任务，重试绝不创建第二个任务。"""
        if not request_id.strip() or not content.strip():
            raise ValueError("消息内容和 request_id 不能为空")
        with self._lock, self._conn:
            prior = self.get_conversation_request(conversation_id, request_id)
            if prior:
                files = self._query("SELECT id FROM attachments WHERE message_id=? ORDER BY id", (prior["message_id"],))
                if (prior["content"] != content or prior["agent_id"] != agent_id
                        or sorted(attachment_ids or []) != [f["id"] for f in files]):
                    raise ValueError("request_id 已用于另一条消息")
                return {"task_id": prior["task_id"], "message_id": prior["message_id"], "created": False}
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError("会话不存在")
            if conversation["deleted_at"]:
                raise ValueError("对话已删除，请先从最近删除中恢复")
            if agent_id not in conversation["agent_ids"]:
                raise ValueError("指定成员不在此会话中")
            for file_id in attachment_ids or []:
                file = self.get_attachment(file_id)
                if not file or file["conversation_id"] != conversation_id or file["message_id"] or file["kind"] != "upload":
                    raise ValueError("附件不存在、已发送或不属于当前会话")
            active = self.get_task(active_task_id) if active_task_id else None
            steering = bool(active and active["conversation_id"] == conversation_id
                            and active["agent_id"] == agent_id
                            and active["status"] in {"queued", "running", "waiting_approval"})
            task_id = active_task_id if steering else _uid("t_")
            message_id, now = _uid("m_"), time.time()
            if not steering:
                self._conn.execute(
                    """INSERT INTO tasks
                       (id,agent_id,title,goal,source,meta,created_at,conversation_id)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (task_id, agent_id, content[:60], content, "conversation",
                     json.dumps(meta or {}, ensure_ascii=False), now, conversation_id),
                )
            self._conn.execute(
                "INSERT INTO conversation_messages (id,conversation_id,agent_id,role,content,task_id,created_at) VALUES (?,?,?,?,?,?,?)",
                (message_id, conversation_id, agent_id, "user", content, task_id, now),
            )
            self._conn.execute(
                "INSERT INTO conversation_requests VALUES (?,?,?,?)",
                (conversation_id, request_id, task_id, message_id),
            )
            for file_id in attachment_ids or []:
                self._conn.execute("UPDATE attachments SET message_id=?,task_id=? WHERE id=?", (message_id, task_id, file_id))
            self._conn.execute("UPDATE conversations SET updated_at=?,unread=0 WHERE id=?", (now, conversation_id))
            return {"task_id": task_id, "message_id": message_id, "created": True, "steering": steering}

    def get_conversation_request(self, conversation_id: str, request_id: str) -> dict | None:
        return self._query_one(
            """SELECT r.task_id,r.message_id,m.agent_id,m.content
               FROM conversation_requests r JOIN conversation_messages m ON m.id=r.message_id
               WHERE r.conversation_id=? AND r.request_id=?""",
            (conversation_id, request_id),
        )

    def add_conversation_message(
        self, conversation_id: str, agent_id: str, role: str, content: str,
        *, task_id: str = "", message_id: str = "", model: str = "", provider: str = "", status: str = "done",
    ) -> str:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("工具活动应写入任务 transcript")
        with self._lock, self._conn:
            if self.get_conversation(conversation_id) is None:
                raise ValueError("会话不存在")
            message_id, now = message_id or _uid("m_"), time.time()
            self._conn.execute(
                """INSERT INTO conversation_messages (id,conversation_id,agent_id,role,content,task_id,created_at,model,provider,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                   model=excluded.model,provider=excluded.provider,status=excluded.status""",
                (message_id, conversation_id, agent_id, role, content, task_id, now, model, provider, status),
            )
            self._conn.execute("UPDATE conversations SET updated_at=?,unread=? WHERE id=?", (now, role == "assistant", conversation_id))
        return message_id

    def list_conversation_messages(self, conversation_id: str) -> list[dict]:
        rows = self._query(
            "SELECT rowid AS seq,* FROM conversation_messages WHERE conversation_id=? ORDER BY created_at,rowid",
            (conversation_id,),
        )
        files = self.list_attachments(conversation_id)
        for row in rows:
            row["attachments"] = [f for f in files if f["message_id"] == row["id"]]
        return rows

    def get_summary(self, conversation_id: str) -> dict | None:
        return self._query_one("SELECT * FROM conversation_summaries WHERE conversation_id=?", (conversation_id,))

    def save_summary(self, conversation_id: str, content: str, through_seq: int, model: str) -> None:
        self._write("""INSERT INTO conversation_summaries VALUES (?,?,?,?,?) ON CONFLICT(conversation_id)
            DO UPDATE SET content=excluded.content,through_seq=excluded.through_seq,model=excluded.model,
            updated_at=excluded.updated_at WHERE excluded.through_seq>=conversation_summaries.through_seq""",
            (conversation_id, content, through_seq, model, time.time()))

    def get_attachment(self, file_id: str) -> dict | None:
        return self._query_one("SELECT * FROM attachments WHERE id=?", (file_id,))

    def list_attachments(self, conversation_id: str) -> list[dict]:
        return self._query("""SELECT id,conversation_id,message_id,task_id,name,mime,size,note,kind,created_at
            FROM attachments WHERE conversation_id=? ORDER BY created_at""", (conversation_id,))

    def add_attachment(self, file: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("""INSERT INTO attachments (id,conversation_id,message_id,task_id,name,mime,size,text,note,kind,created_at)
                VALUES (:id,:conversation_id,:message_id,:task_id,:name,:mime,:size,:text,:note,:kind,:created_at)""", file)

    def list_conversation_tasks(self, conversation_id: str) -> list[dict]:
        return self._query(
            "SELECT * FROM tasks WHERE conversation_id=? ORDER BY created_at", (conversation_id,)
        )

    def list_conversation_approvals(self, conversation_id: str) -> list[dict]:
        rows = self._query(
            """SELECT a.* FROM approvals a JOIN tasks t ON t.id=a.task_id
               WHERE t.conversation_id=? ORDER BY a.created_at""", (conversation_id,)
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    # ---------------- 任务模型消息 ----------------

    def add_message(
        self,
        task_id: str,
        agent_id: str,
        role: str,
        content: str,
        *,
        tool_name: str = "",
        step: int = 0,
    ) -> None:
        self._write(
            """INSERT INTO messages (id, task_id, agent_id, role, content, tool_name, step, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (_uid("m_"), task_id, agent_id, role, content, tool_name, step, time.time()),
        )

    def list_messages(self, task_id: str) -> list[dict]:
        return self._query(
            "SELECT * FROM messages WHERE task_id = ? ORDER BY step ASC, created_at ASC",
            (task_id,),
        )

    # ---------------- 事件 ----------------

    def add_event(
        self, type_: str, payload: dict | None = None, *, task_id: str = "", agent_id: str = ""
    ) -> dict:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events (task_id, agent_id, type, payload, created_at) VALUES (?,?,?,?,?)",
                (task_id, agent_id, type_, json.dumps(payload or {}, ensure_ascii=False), now),
            )
            event_id = cursor.lastrowid
        return {"id": event_id, "type": type_, "task_id": task_id, "agent_id": agent_id,
                "payload": payload or {}, "created_at": now}

    def list_events(self, *, after_id: int = 0, task_id: str | None = None,
                    limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)
        for row in rows:
            try:
                row["payload"] = json.loads(row["payload"])
            except json.JSONDecodeError:
                row["payload"] = {}
        return rows

    def max_event_id(self) -> int:
        row = self._query_one("SELECT COALESCE(MAX(id), 0) AS n FROM events")
        return int(row["n"]) if row else 0

    # ---------------- 记忆 ----------------

    def remember(self, agent_id: str, key: str, value: str) -> None:
        if not key.strip() or len(key) > 160 or not value.strip() or len(value) > 20000:
            raise ValueError("记忆名称需为 1–160 字符，内容为 1–20000 字符")
        now = time.time()
        self._write(
            """INSERT INTO memory (agent_id, key, value, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(agent_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (agent_id, key, value, now, now),
        )

    def recall(self, agent_id: str, key: str | None = None) -> list[dict]:
        if key:
            row = self._query_one(
                "SELECT * FROM memory WHERE agent_id = ? AND key = ?", (agent_id, key)
            )
            return [row] if row else []
        return self._query(
            "SELECT * FROM memory WHERE agent_id = ? ORDER BY updated_at DESC", (agent_id,)
        )

    def forget(self, agent_id: str, key: str) -> None:
        self._write("DELETE FROM memory WHERE agent_id = ? AND key = ?", (agent_id, key))

    # ---------------- 人工确认 ----------------

    def create_approval(
        self,
        *,
        task_id: str,
        agent_id: str,
        kind: str,
        summary: str,
        detail: dict | None = None,
    ) -> str:
        approval_id = _uid("a_")
        self._write(
            """INSERT INTO approvals (id, task_id, agent_id, kind, summary, detail, status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                approval_id,
                task_id,
                agent_id,
                kind,
                summary,
                json.dumps(detail or {}, ensure_ascii=False),
                "pending",
                time.time(),
            ),
        )
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row:
            try:
                row["detail"] = json.loads(row["detail"])
            except json.JSONDecodeError:
                row["detail"] = {}
        return row

    def list_approvals(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)
        for row in rows:
            try:
                row["detail"] = json.loads(row["detail"])
            except json.JSONDecodeError:
                row["detail"] = {}
        return rows

    def decide_approval(self, approval_id: str, *, approved: bool, note: str = "") -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET status=?, note=?, decided_at=? WHERE id=? AND status='pending'",
                ("approved" if approved else "rejected", note, time.time(), approval_id),
            )
            return cursor.rowcount == 1

    def pending_approval_count(self) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'"
        )
        return int(row["n"]) if row else 0

    # ---------------- 用量 ----------------

    def log_usage(
        self,
        *,
        task_id: str,
        agent_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_known: bool = True,
        tokens_known: bool = True,
    ) -> None:
        self._write(
            """INSERT INTO usage_log
               (task_id, agent_id, provider, model, prompt_tokens, completion_tokens, cost_usd,
                cost_known, tokens_known, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, agent_id, provider, model, prompt_tokens, completion_tokens,
             cost_usd if cost_known else 0.0, int(cost_known), int(tokens_known), time.time()),
        )

    def spend_today(self) -> float:
        start = time.time() - 86400
        row = self._query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage_log WHERE created_at >= ? AND cost_known=1",
            (start,),
        )
        return round(float(row["s"]), 6) if row else 0.0

    def usage_summary(self, *, days: int = 7) -> list[dict]:
        start = time.time() - days * 86400
        return self._query(
            """SELECT agent_id, provider, model,
                      COUNT(*) AS calls,
                      SUM(prompt_tokens) AS prompt_tokens,
                      SUM(completion_tokens) AS completion_tokens,
                      SUM(cost_usd) AS cost_usd,
                      MIN(cost_known) AS cost_known,
                      SUM(CASE WHEN cost_known=0 THEN 1 ELSE 0 END) AS unknown_cost_calls
               FROM usage_log WHERE created_at >= ?
               GROUP BY agent_id, provider, model
               ORDER BY cost_usd DESC""",
            (start,),
        )

    # ---------------- 运维 ----------------

    def stats(self) -> dict:
        tasks = self._query_one(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                      SUM(CASE WHEN status='queued'  THEN 1 ELSE 0 END) AS queued,
                      SUM(CASE WHEN status='done'    THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed
               FROM tasks"""
        ) or {}
        return {
            "tasks": {k: int(v or 0) for k, v in tasks.items()},
            "spend_today_usd": self.spend_today(),
            "db_path": str(self.path),
        }

    def mark_stale_running_as_failed(self) -> int:
        """启动时结束遗留任务及审批；不自动重跑可能已经产生副作用的操作。"""
        rows = self._query(
            "SELECT * FROM tasks WHERE status IN ('queued','running','waiting_approval')"
        )
        reason = "后端重启，任务已中断。请核对已完成的操作后重新发起。"
        for row in rows:
            self.set_task_status(row["id"], "failed", error=reason)
            if row["conversation_id"]:
                self.add_conversation_message(row["conversation_id"], row["agent_id"],
                                              "system", reason, task_id=row["id"])
            self.add_event("task.failed", {"error": reason, "conversation_id": row["conversation_id"]},
                           task_id=row["id"], agent_id=row["agent_id"])
        return len(rows)
