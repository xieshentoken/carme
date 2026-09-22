"""持久化：SQLite 存任务、消息、事件、记忆、用量。

选 SQLite 而不是 Postgres 是因为目标机器只有 8GB 内存，
单文件、零运维、备份就是拷一个文件。个人团队这个量级完全够。
"""

from __future__ import annotations

import json
import hashlib
import math
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    key_fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

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
CREATE INDEX IF NOT EXISTS idx_attachments_kind_time ON attachments(kind, mime, created_at DESC);
"""

TERMINAL_STATUSES = ("done", "failed", "cancelled")

M4_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (name TEXT PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_versions (
    scope TEXT NOT NULL, scope_id TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
    value TEXT NOT NULL, source_task_id TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL,
    constraint_flag INTEGER NOT NULL DEFAULT 0, expires_at REAL, revoked INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, PRIMARY KEY(scope,scope_id,key,version)
);
CREATE TABLE IF NOT EXISTS memory_acl (
    bot_id TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL,
    can_read INTEGER NOT NULL DEFAULT 0, can_write INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(bot_id,scope,scope_id)
);
CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY(task_id,seq)
);
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS task_outcomes (
    task_id TEXT PRIMARY KEY, status TEXT NOT NULL, report TEXT NOT NULL,
    user_accepted INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_operations (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action_digest TEXT NOT NULL,
    tool TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
    receipt TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(task_id,action_digest)
);
CREATE TABLE IF NOT EXISTS task_artifacts (
    task_id TEXT NOT NULL, artifact_id TEXT NOT NULL, sha256 TEXT NOT NULL,
    bot_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(task_id,artifact_id)
);

-- 显式转交：用户要求时，一个 Bot 把会话里的附件交给另一个 Bot（仍限同一会话）。
CREATE TABLE IF NOT EXISTS attachment_shares (
    conversation_id TEXT NOT NULL, artifact_id TEXT NOT NULL, bot_id TEXT NOT NULL,
    granted_by TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(conversation_id,artifact_id,bot_id)
);
CREATE TABLE IF NOT EXISTS artifact_validations (
    id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, sha256 TEXT NOT NULL,
    task_id TEXT NOT NULL, validator_run TEXT NOT NULL, checks_hash TEXT NOT NULL,
    status TEXT NOT NULL, report TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_tool_budget (
    root_task_id TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0
);
"""


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"

def _number(value, fallback: float) -> float:
    """导入外部数据时把时间戳收敛为有限浮点数，坏值退回 fallback。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if result == result and abs(result) != float("inf") else fallback


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
            self._conn.executescript(M4_SCHEMA)
            attachment_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(attachments)")}
            for name in ("sha256", "provenance"):
                if name not in attachment_columns:
                    self._conn.execute(f"ALTER TABLE attachments ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
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

            # Additive migration: retain legacy rows and their timestamps. Never re-import
            # a revoked fact on a later restart.
            if not self._conn.execute("SELECT 1 FROM schema_versions WHERE name='m4_memory'").fetchone():
                self._conn.execute("""INSERT OR IGNORE INTO memory_versions
                    (scope,scope_id,key,version,value,actor,constraint_flag,created_at)
                    SELECT CASE WHEN agent_id='__shared__' THEN 'user' ELSE 'bot' END,
                           CASE WHEN agent_id='__shared__' THEN 'shared' ELSE agent_id END,
                           key,1,value,'legacy',CASE WHEN agent_id='__shared__' THEN 1 ELSE 0 END,updated_at
                    FROM memory""")
                self._conn.execute("INSERT OR IGNORE INTO schema_versions VALUES ('m4_memory',?)", (time.time(),))
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
            # 先把 WAL 合并回主库再关连接：容器被强杀时也不会留下半截 WAL。
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def create_session(self, key: str, *, ttl: int = 604800) -> tuple[str, dict]:
        token, now = "cs_" + secrets.token_urlsafe(32), time.time()
        session_id = _uid("s_")
        self._write("INSERT INTO browser_sessions VALUES (?,?,?,?,?,0)",
                    (session_id, hashlib.sha256(token.encode()).hexdigest(),
                     hashlib.sha256(key.encode()).hexdigest(), now, now + ttl))
        return token, {"id": session_id, "created_at": now, "expires_at": now + ttl}

    def session(self, token: str, key: str) -> dict | None:
        return self._query_one("SELECT id,created_at,expires_at FROM browser_sessions "
                               "WHERE token_hash=? AND key_fingerprint=? AND revoked=0 AND expires_at>?",
                               (hashlib.sha256(token.encode()).hexdigest(),
                                hashlib.sha256(key.encode()).hexdigest(), time.time()))

    def revoke_session(self, session_id: str) -> None:
        self._write("UPDATE browser_sessions SET revoked=1 WHERE id=?", (session_id,))

    def list_sessions(self, key: str) -> list[dict]:
        return self._query("SELECT id,created_at,expires_at FROM browser_sessions "
                           "WHERE key_fingerprint=? AND revoked=0 AND expires_at>?",
                           (hashlib.sha256(key.encode()).hexdigest(), time.time()))

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
        max_daily_tasks: int = 0,
    ) -> str:
        task_id = _uid("t_")
        now = time.time()
        if parent_id and not conversation_id:
            parent = self.get_task(parent_id)
            conversation_id = (parent or {}).get("conversation_id", "")
        with self._lock, self._conn:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            self._check_daily_tasks(max_daily_tasks, now)
            self._conn.execute(
                """INSERT INTO tasks (id, agent_id, parent_id, title, goal, status, source, meta, created_at, conversation_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (task_id, agent_id, parent_id, title or goal[:60], goal, "queued", source,
                 json.dumps(meta or {}, ensure_ascii=False), now, conversation_id),
            )
        return task_id

    def _check_daily_tasks(self, limit: int, now: float) -> None:
        # Called while holding the same transaction as the task INSERT.
        if limit and self._conn.execute("SELECT count(*) FROM tasks WHERE created_at>=?",
                                       (int(now // 86400) * 86400,)).fetchone()[0] >= limit:
            raise ValueError("max_daily_tasks_exceeded")

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
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                               (status, result, error if status == "failed" else "", now, task_id))
            if status in TERMINAL_STATUSES:
                self._conn.execute("UPDATE approvals SET status='rejected',note=?,decided_at=? WHERE task_id=? AND status='pending'",
                                   (error or "任务已结束，此审批不再有效", now, task_id))

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
               WHERE m.conversation_id=c.id AND m.status != 'streaming'
               ORDER BY created_at DESC, rowid DESC LIMIT 1)
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
        max_daily_tasks: int = 0,
    ) -> dict:
        """同一请求原子地保存用户消息和任务，重试绝不创建第二个任务。"""
        if not request_id.strip() or not content.strip():
            raise ValueError("消息内容和 request_id 不能为空")
        with self._lock, self._conn:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
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
                self._check_daily_tasks(max_daily_tasks, now)
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
        return self._query("""SELECT id,conversation_id,message_id,task_id,name,mime,size,note,kind,created_at,sha256,provenance
            FROM attachments WHERE conversation_id=? ORDER BY created_at""", (conversation_id,))
    def latest_png_artifact(self) -> dict | None:
        """最新一张 Docker Browser 截图（kind=browser_shot 的 PNG）；给浏览器画面降级视图用。"""
        return self._query_one("""SELECT id,conversation_id,message_id,task_id,name,mime,size,note,kind,created_at,sha256,provenance
            FROM attachments WHERE kind='browser_shot' AND mime='image/png' ORDER BY created_at DESC LIMIT 1""")

    def add_attachment(self, file: dict) -> None:
        file = {"sha256": "", "provenance": "", **file}
        with self._lock, self._conn:
            self._conn.execute("""INSERT INTO attachments (id,conversation_id,message_id,task_id,name,mime,size,text,note,kind,created_at,sha256,provenance)
                VALUES (:id,:conversation_id,:message_id,:task_id,:name,:mime,:size,:text,:note,:kind,:created_at,:sha256,:provenance)""", file)

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
        self.memory_write('admin', 'user' if agent_id=='__shared__' else 'bot',
                          'shared' if agent_id=='__shared__' else agent_id, key,value,
                          constraint=agent_id=='__shared__',admin=True)

    def recall(self, agent_id: str, key: str | None = None) -> list[dict]:
        scope,scope_id=('user','shared') if agent_id=='__shared__' else ('bot',agent_id)
        return self._query('''SELECT m.* FROM memory m JOIN memory_versions v ON v.scope=? AND v.scope_id=? AND v.key=m.key
            WHERE m.agent_id=? AND (? IS NULL OR m.key=?) AND v.revoked=0 AND (v.expires_at IS NULL OR v.expires_at>?)
            AND v.version=(SELECT MAX(n.version) FROM memory_versions n WHERE n.scope=v.scope AND n.scope_id=v.scope_id AND n.key=v.key)
            ORDER BY m.updated_at DESC''',(scope,scope_id,agent_id,key,key,time.time()))

    def forget(self, agent_id: str, key: str) -> None:
        self.memory_write('admin','user' if agent_id=='__shared__' else 'bot',
                          'shared' if agent_id=='__shared__' else agent_id,key,'',revoke=True,admin=True)

    # ---------------- 迁移：导出 / 导入 ----------------

    def memory_grant(self, bot_id: str, scope: str, scope_id: str, *, read=False, write=False) -> None:
        if scope not in {'user', 'project'} or not bot_id or not scope_id:
            raise ValueError('invalid_memory_grant')
        self._write('INSERT OR REPLACE INTO memory_acl VALUES (?,?,?,?,?)',
                    (bot_id, scope, scope_id, int(read or write), int(write)))

    def memory_allowed(self, bot_id: str, scope: str, scope_id: str, *, task_id='', write=False) -> bool:
        if scope == 'bot':
            return scope_id == bot_id
        if scope == 'task':
            task = self.get_task(task_id) if task_id == scope_id else None
            return bool(task and task['agent_id'] == bot_id)
        if scope not in {'user', 'project'}:
            return False
        if scope == 'user' and scope_id == 'shared' and not write:
            return True  # Explicit legacy team namespace; never grants shared writes.
        row = self._query_one('SELECT * FROM memory_acl WHERE bot_id=? AND scope=? AND scope_id=?',
                              (bot_id, scope, scope_id))
        return bool(row and row['can_write' if write else 'can_read'])

    def memory_write(self, bot_id: str, scope: str, scope_id: str, key: str, value: str, *,
                     task_id='', expected_version: int | None = None, constraint=False,
                     expires_at: float | None = None, revoke=False, admin=False) -> dict:
        if not admin and not self.memory_allowed(bot_id, scope, scope_id, task_id=task_id, write=True):
            raise ValueError('memory_write_denied')
        if (scope not in {'user','project','bot','task'} or not scope_id or len(scope_id)>128
                or not key.strip() or len(key)>160 or len(value)>20000 or (not value.strip() and not revoke)):
            raise ValueError('invalid_memory_fact')
        if expires_at is not None and (not math.isfinite(expires_at) or expires_at <= time.time()):
            raise ValueError('invalid_memory_expiry')
        with self._lock, self._conn:
            if not self._conn.in_transaction:
                self._conn.execute('BEGIN IMMEDIATE')
            row = self._conn.execute('SELECT version FROM memory_versions WHERE scope=? AND scope_id=? AND key=? ORDER BY version DESC LIMIT 1',
                                     (scope,scope_id,key)).fetchone()
            version = row[0] if row else 0
            if expected_version != version and not (expected_version is None and (not version or admin)):
                raise ValueError('memory_version_conflict')
            now = time.time()
            self._conn.execute('INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (scope,scope_id,key,version+1,value,task_id,'admin' if admin else bot_id,
                 int(constraint),expires_at,int(revoke),now))
            owner = scope_id if scope=='bot' else '__shared__' if (scope,scope_id)==('user','shared') else None
            if owner:
                if revoke:
                    self._conn.execute('DELETE FROM memory WHERE agent_id=? AND key=?',(owner,key))
                else:
                    self._conn.execute('''INSERT INTO memory VALUES (?,?,?,?,?) ON CONFLICT(agent_id,key)
                        DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',(owner,key,value,now,now))
        return {'scope':scope,'scope_id':scope_id,'key':key,'version':version+1,'revoked':bool(revoke)}

    def memory_search(self, bot_id: str, *, task_id='', key=None, query='', scope=None, offset=0, limit=100) -> list[dict]:
        # ACL predicates run in SQLite before query matching or any body is returned.
        return self._query('''SELECT m.* FROM memory_versions m WHERE
            m.version=(SELECT MAX(v.version) FROM memory_versions v WHERE v.scope=m.scope AND v.scope_id=m.scope_id AND v.key=m.key)
            AND m.revoked=0 AND (m.expires_at IS NULL OR m.expires_at>?)
            AND ((m.scope='bot' AND m.scope_id=?) OR (m.scope='user' AND m.scope_id='shared')
              OR (m.scope='task' AND m.scope_id=? AND EXISTS(SELECT 1 FROM tasks t WHERE t.id=? AND t.agent_id=?))
              OR EXISTS(SELECT 1 FROM memory_acl a WHERE a.bot_id=? AND a.scope=m.scope AND a.scope_id=m.scope_id AND a.can_read=1))
            AND (? IS NULL OR m.scope=?) AND (? IS NULL OR m.key=?) AND (?='' OR instr(lower(m.key||' '||m.value),lower(?))>0)
            ORDER BY m.constraint_flag DESC,m.created_at DESC,m.scope,m.scope_id,m.key LIMIT ? OFFSET ?''',
            (time.time(),bot_id,task_id,task_id,bot_id,bot_id,scope,scope,key,key,query,query,min(200,max(1,limit)),max(0,offset)))

    def memory_context(self, bot_id: str, *, task_id='') -> tuple[str,list[dict]]:
        # Independent pools: private rows cannot crowd out shared/project constraints.
        pools = {'constraints':(12000,20),'user':(4000,20),'project':(4000,20),'bot':(8000,40),'task':(4000,20)}
        used = {name:[0,0] for name in pools}; lines=[]; refs=[]
        # Separate per-scope queries also keep >200 private facts out of the shared quota.
        rows=[r for scope in ('user','project','bot','task')
              for r in self.memory_search(bot_id,task_id=task_id,scope=scope,limit=200)]
        seen=set()
        for row in rows:
            identity=(row['scope'],row['scope_id'],row['key'],row['version'])
            if identity in seen:continue
            seen.add(identity); pool='constraints' if row['constraint_flag'] else row['scope']
            text=f"- [{row['scope']}:{row['scope_id']} v{row['version']}] {row['key']}: {row['value']}"
            if used[pool][0]+len(text)>pools[pool][0] or used[pool][1]>=pools[pool][1]:
                if row['constraint_flag']:raise ValueError('memory_constraint_budget_exceeded: narrow authorized constraints')
                continue
            used[pool][0]+=len(text);used[pool][1]+=1;lines.append(text)
            refs.append({k:row[k] for k in ('scope','scope_id','key','version')})
        return ('\n\n[分层记忆预览；完整值、来源及更多内容请用 recall]\n'+'\n'.join(lines) if lines else ''),refs

    def memory_refs_valid(self, refs: list[dict]) -> bool:
        for ref in refs:
            row=self._query_one('SELECT * FROM memory_versions WHERE scope=? AND scope_id=? AND key=? ORDER BY version DESC LIMIT 1',
                                (ref['scope'],ref['scope_id'],ref['key']))
            if not row or row['version']!=ref['version'] or row['revoked'] or (row['expires_at'] is not None and row['expires_at']<=time.time()):
                return False
        return True

    def update_task_meta(self, task_id: str, changes: dict) -> dict:
        with self._lock, self._conn:
            row=self._conn.execute('SELECT meta FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:raise ValueError('task_missing')
            meta=json.loads(row[0]);meta.update(changes)
            self._conn.execute('UPDATE tasks SET meta=? WHERE id=?',(json.dumps(meta,ensure_ascii=False),task_id))
        return meta

    def checkpoint(self, task_id: str, payload: dict | None = None) -> dict | None:
        if payload is None:
            row=self._query_one('SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY seq DESC LIMIT 1',(task_id,))
            if not row:return None
            if hashlib.sha256(row['payload'].encode()).hexdigest()!=row['sha256']:raise ValueError('checkpoint_hash_conflict')
            return json.loads(row['payload'])
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False)
        if len(raw.encode())>2*1024*1024:raise ValueError('checkpoint_too_large')
        with self._lock,self._conn:
            seq=self._conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM task_checkpoints WHERE task_id=?',(task_id,)).fetchone()[0]
            self._conn.execute('INSERT INTO task_checkpoints VALUES (?,?,?,?,?)',(task_id,seq,raw,hashlib.sha256(raw.encode()).hexdigest(),time.time()))
        return payload

    def begin_run(self, task_id: str) -> str:
        rid='r_'+uuid.uuid4().hex
        self._write('INSERT INTO task_runs VALUES (?,?,?, ?,NULL)',(rid,task_id,'running',time.time()))
        self.update_task_meta(task_id,{'attempt_id':rid})
        return rid

    def finish_run(self, run_id: str, status: str) -> None:
        self._write('UPDATE task_runs SET status=?,finished_at=? WHERE id=?',(status,time.time(),run_id))

    def operation_begin(self, task_id: str, tool: str, action_digest: str) -> dict:
        with self._lock,self._conn:
            if not self._conn.in_transaction:self._conn.execute('BEGIN IMMEDIATE')
            row=self._conn.execute('SELECT * FROM task_operations WHERE task_id=? AND action_digest=?',(task_id,action_digest)).fetchone()
            if row:
                result=dict(row)
                if result['status']=='retry_authorized':
                    self._conn.execute("UPDATE task_operations SET status='pending',updated_at=? WHERE id=?",(time.time(),result['id']))
                    result['status']='new'
                return result
            oid='op_'+uuid.uuid4().hex;now=time.time()
            self._conn.execute('INSERT INTO task_operations VALUES (?,?,?,?,?,?,?,?,?)',
                (oid,task_id,action_digest,tool,'pending','','{}',now,now))
        return {'id':oid,'status':'new'}

    def operation_finish(self, operation_id: str, result: str, *, receipt=None) -> None:
        self._write("UPDATE task_operations SET status='finished',result=?,receipt=?,updated_at=? WHERE id=? AND status='pending'",
                    (result,json.dumps(receipt or {}),time.time(),operation_id))

    def operation_reconcile(self, task_id: str, operation_id: str, *, effect: str, receipt: dict) -> None:
        if effect not in {'confirmed','not_performed'} or not receipt or len(json.dumps(receipt))>8000:
            raise ValueError('explicit_reconciliation_receipt_required')
        operation=self._query_one('SELECT * FROM task_operations WHERE id=? AND task_id=?',(operation_id,task_id))
        if not operation or operation['status']!='pending':raise ValueError('operation_not_pending')
        status='finished' if effect=='confirmed' else 'retry_authorized'
        self._write('UPDATE task_operations SET status=?,result=?,receipt=?,updated_at=? WHERE id=?',
                    (status,'[已由管理员对账；不得重复执行] '+json.dumps(receipt,ensure_ascii=False),json.dumps(receipt),time.time(),operation_id))

    def claim_tool_budget(self, task_id: str) -> None:
        with self._lock,self._conn:
            if not self._conn.in_transaction:self._conn.execute('BEGIN IMMEDIATE')
            task=self._conn.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task:return
            meta=json.loads(task['meta']);envelope=meta.get('envelope')
            if not envelope:return
            ancestor=task
            while ancestor:
                contract=json.loads(ancestor['meta']).get('envelope')
                if contract:
                    ceiling=contract['budget']['tool_calls']
                    self._conn.execute('INSERT OR IGNORE INTO task_tool_budget VALUES (?,0)',(ancestor['id'],))
                    cursor=self._conn.execute('UPDATE task_tool_budget SET used=used+1 WHERE root_task_id=? AND used<?',(ancestor['id'],ceiling))
                    if cursor.rowcount!=1:raise ValueError('parent_child_tool_budget_exceeded')
                if not ancestor['parent_id']:break
                ancestor=self._conn.execute('SELECT * FROM tasks WHERE id=?',(ancestor['parent_id'],)).fetchone()
                if ancestor is None:raise ValueError('parent_task_missing')

    def artifact_link(self, task_id: str, artifact_id: str, *, source: str) -> None:
        task=self.get_task(task_id);file=self.get_attachment(artifact_id)
        if not task or not file or task['conversation_id']!=file['conversation_id']:
            raise ValueError('artifact_scope_denied')
        from .attachments import file_path
        actual=hashlib.sha256(file_path(self,artifact_id).read_bytes()).hexdigest()
        if file['sha256'] and file['sha256']!=actual:raise ValueError('artifact_hash_conflict')
        with self._lock,self._conn:
            self._conn.execute('INSERT OR IGNORE INTO task_artifacts VALUES (?,?,?,?,?)',(task_id,artifact_id,actual,task['agent_id'],source))

    def attachment_owner_bot(self, file: dict) -> str:
        """附件归属哪个 Bot：先看它随哪条消息发给了谁，再看产出它的任务。

        附件按 Bot 隔离，所以「谁的附件」必须由稳定归属判定回答，而不是由当前任务决定。
        """
        message_id = file.get("message_id") or ""
        if message_id:
            row = self._query_one("SELECT agent_id FROM conversation_messages WHERE id=?", (message_id,))
            if row and row["agent_id"]:
                return row["agent_id"]
        task_id = file.get("task_id") or ""
        task = self.get_task(task_id) if task_id else None
        return task["agent_id"] if task else ""

    def attachment_visible_to(self, task: dict, file: dict) -> bool:
        """该附件的 Bot 级可见性：发给它的、它自己产出的、被显式转交给它的。"""
        if self.attachment_owner_bot(file) == task["agent_id"]:
            return True
        return bool(self._query_one(
            "SELECT 1 FROM attachment_shares WHERE conversation_id=? AND artifact_id=? AND bot_id=?",
            (task["conversation_id"], file["id"], task["agent_id"])))

    def attachment_share(self, task_id: str, artifact_id: str, bot_id: str, *, revoke: bool = False) -> dict:
        """把一个附件显式转交给同一会话里的另一个 Bot；调用方必须自己读得到它。"""
        task = self.get_task(task_id)
        conversation = self.get_conversation(task["conversation_id"]) if task else None
        if not task or not conversation:
            raise ValueError("artifact_scope_denied")
        if bot_id not in conversation["agent_ids"]:
            raise ValueError("share_target_not_in_conversation")
        if bot_id == task["agent_id"]:
            raise ValueError("share_target_is_self")
        file = self.artifact_access(task_id, artifact_id)
        with self._lock, self._conn:
            if revoke:
                self._conn.execute("DELETE FROM attachment_shares WHERE conversation_id=? AND artifact_id=? AND bot_id=?",
                                   (task["conversation_id"], artifact_id, bot_id))
            else:
                self._conn.execute("INSERT OR IGNORE INTO attachment_shares VALUES (?,?,?,?,?)",
                                   (task["conversation_id"], artifact_id, bot_id, task_id, time.time()))
        return {"artifact_id": artifact_id, "bot_id": bot_id, "name": file["name"],
                "revoked": bool(revoke)}

    def artifact_access(self, task_id: str, artifact_id: str) -> dict:
        task=self.get_task(task_id);file=self.get_attachment(artifact_id)
        if not task or not file or not task['conversation_id'] or file['conversation_id']!=task['conversation_id']:
            raise ValueError('artifact_scope_denied')
        meta=json.loads(task['meta'])
        # 归属或显式转交决定可见性；任务级授权行只是其中一种来源。
        allowed=file['task_id']==task_id or self.attachment_visible_to(task,file)
        if meta.get('envelope'):
            from .attachments import file_path
            actual=hashlib.sha256(file_path(self,artifact_id).read_bytes()).hexdigest()
            if file['sha256'] and file['sha256']!=actual:raise ValueError('artifact_version_conflict')
            grant=self._query_one('SELECT * FROM task_artifacts WHERE task_id=? AND artifact_id=? AND bot_id=?',(task_id,artifact_id,task['agent_id']))
            if grant:
                if grant['sha256']!=actual:raise ValueError('artifact_version_conflict')
            elif not allowed:
                raise ValueError('artifact_version_not_granted')
        elif not allowed:
            raise ValueError('artifact_version_not_granted' if file['message_id'] else 'artifact_not_sent')
        return file

    def outcome(self, task_id: str) -> dict:
        row=self._query_one('SELECT * FROM task_outcomes WHERE task_id=?',(task_id,))
        if not row:return {'task_id':task_id,'status':'unverified','user_accepted':False,'report':{}}
        row['report']=json.loads(row['report']);row['user_accepted']=bool(row['user_accepted']);return row

    def set_outcome(self, task_id: str, status: str, report: dict) -> None:
        if status not in {'unverified','failed','verified'}:raise ValueError('outcome_status_denied')
        self._write('''INSERT INTO task_outcomes VALUES (?,?,?,0,?) ON CONFLICT(task_id)
            DO UPDATE SET status=excluded.status,report=excluded.report,user_accepted=0,updated_at=excluded.updated_at''',
            (task_id,status,json.dumps(report,ensure_ascii=False),time.time()))

    def accept_outcome(self, task_id: str, report_hash: str) -> None:
        from .security import digest
        outcome=self.outcome(task_id)
        if outcome['status']!='verified' or digest(outcome['report'])!=report_hash:raise ValueError('verified_outcome_binding_required')
        from .attachments import file_path
        for artifact in outcome['report'].get('artifacts',[]):
            if hashlib.sha256(file_path(self,artifact['id']).read_bytes()).hexdigest()!=artifact['sha256']:
                raise ValueError('artifact_version_conflict')
        self._write('UPDATE task_outcomes SET user_accepted=1,updated_at=? WHERE task_id=?',(time.time(),task_id))

    def conversations_for_agents(self, agent_ids: list[str], *, include_deleted: bool = False) -> list[dict]:
        """返回与给定成员相关的会话，用于导出。"""
        wanted = [value for value in dict.fromkeys(agent_ids) if value]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        clause = "" if include_deleted else "AND c.deleted_at = 0"
        rows = self._query(
            f"SELECT c.* FROM conversations c WHERE EXISTS "
            f"(SELECT 1 FROM json_each(c.agent_ids) WHERE json_each.value IN ({placeholders})) {clause} "
            f"ORDER BY c.created_at ASC",
            tuple(wanted),
        )
        for row in rows:
            row["agent_ids"] = json.loads(row["agent_ids"])
        return rows

    def import_memory(self, agent_id: str, entries: list[dict]) -> int:
        """按导出内容写入记忆，保留原始时间戳；同 key 覆盖。返回写入条数。"""
        now = time.time()
        rows = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            value = str(entry.get("value") or "").strip()
            if not key or not value or len(key) > 160 or len(value) > 20000:
                continue
            rows.append((agent_id, key, value, _number(entry.get("created_at"), now),
                         _number(entry.get("updated_at"), now)))
        if not rows:
            return 0
        statement = ("INSERT INTO memory (agent_id, key, value, created_at, updated_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(agent_id, key) DO UPDATE SET value = excluded.value, "
                     "updated_at = excluded.updated_at")
        with self._lock, self._conn:
            self._conn.executemany(statement, rows)
            for owner,key,value,created,updated in rows:
                scope,scope_id=('user','shared') if owner=='__shared__' else ('bot',owner)
                version=self._conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM memory_versions WHERE scope=? AND scope_id=? AND key=?',
                                           (scope,scope_id,key)).fetchone()[0]
                self._conn.execute('INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (scope,scope_id,key,version,value,'','import',int(owner=='__shared__'),None,0,updated))
        return len(rows)

    def import_conversation(self, payload: dict) -> dict | None:
        """按导出内容新建会话与消息。

        始终生成新的会话 id：导入是「并入」本机已有数据，绝不复用来源设备的 id，
        也不会覆盖本机会话。任务、审批与事件不导出，因此也不导入。
        """
        members = list(dict.fromkeys(str(value) for value in (payload.get("agent_ids") or []) if str(value or "").strip()))[:6]
        if not members:
            return None
        now = time.time()
        created_at = _number(payload.get("created_at"), now)
        updated_at = _number(payload.get("updated_at"), created_at)
        conversation_id = _uid("c_")
        message_sql = ("INSERT INTO conversation_messages "
                       "(id, conversation_id, agent_id, role, content, task_id, created_at, model, provider, status) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)")
        summary_sql = ("INSERT INTO conversation_summaries (conversation_id, content, through_seq, model, updated_at) "
                       "VALUES (?,?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET content = excluded.content, "
                       "model = excluded.model, updated_at = excluded.updated_at")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO conversations (id, title, agent_ids, kind, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (conversation_id, str(payload.get("title") or "")[:200],
                 json.dumps(members, ensure_ascii=False),
                 "group" if len(members) > 1 else "direct", created_at, updated_at),
            )
            for message in payload.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = str(message.get("content") or "")
                if role not in {"user", "assistant", "system"} or not content:
                    continue
                self._conn.execute(message_sql, (
                    _uid("m_"), conversation_id, str(message.get("agent_id") or ""), role, content, "",
                    _number(message.get("created_at"), created_at), str(message.get("model") or ""),
                    str(message.get("provider") or ""), "done",
                ))
            summary = payload.get("summary") or {}
            if isinstance(summary, dict) and str(summary.get("content") or "").strip():
                self._conn.execute(summary_sql, (
                    conversation_id, str(summary["content"]), int(_number(summary.get("through_seq"), 0)),
                    str(summary.get("model") or ""), _number(summary.get("updated_at"), now),
                ))
        return self.get_conversation(conversation_id)

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

    def usage_by_provider(self) -> dict:
        """按 provider 汇总 token 消耗；只累加 tokens_known=1 的行，避免把未知用量当成 0 混入平均。"""
        rows = self._query(
            """SELECT provider,
                      SUM(CASE WHEN tokens_known=1 THEN prompt_tokens ELSE 0 END) AS input,
                      SUM(CASE WHEN tokens_known=1 THEN completion_tokens ELSE 0 END) AS output,
                      COUNT(*) AS requests,
                      SUM(CASE WHEN tokens_known=1 THEN 1 ELSE 0 END) AS known,
                      MAX(created_at) AS last
               FROM usage_log WHERE provider <> '' GROUP BY provider"""
        )
        return {row["provider"]: {"input": int(row["input"] or 0), "output": int(row["output"] or 0),
                                  "requests": int(row["requests"] or 0), "known": int(row["known"] or 0),
                                  "last": float(row["last"] or 0)}
                for row in rows}

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
        self._write("UPDATE task_runs SET status='interrupted',finished_at=? WHERE status='running'",(time.time(),))
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
