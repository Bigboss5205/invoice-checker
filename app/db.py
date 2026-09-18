"""SQLite 状态存储。

为什么用 SQLite 而不是内存字典
------------------------------
「哪张票查过了、结果是什么、哪张卡在等人工验证码」，
这些在容器重启后必须还在——否则重跑一遍会浪费平台的查验次数。

并发说明
--------
单进程、单 SQLite 连接、一把线程锁。查验任务本身是串行的，
查询量也只有网页轮询这个级别，这个方案足够，而且没有额外依赖。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  任务状态机
# ---------------------------------------------------------------------------
#  pending           已入库，排队等处理
#  extracting        正在抽字段
#  needs_fields      字段抽不全，等人工补录
#  ready             字段齐了，等查验
#  verifying         正在查验
#  awaiting_captcha  正在等人工输入验证码
#  done              查验完成（结论在 verify_status）
#  error             处理出错（可重试）
STATUSES = (
    "pending", "extracting", "needs_fields", "ready",
    "verifying", "awaiting_captcha", "done", "error",
)

# 进程重启后需要回滚的状态：它们代表「当时有协程正在做这件事」
IN_FLIGHT = ("extracting", "verifying", "awaiting_captcha")

STATUS_LABELS = {
    "pending": "排队中",
    "extracting": "解析中",
    "needs_fields": "待补录",
    "ready": "待查验",
    "verifying": "查验中",
    "awaiting_captcha": "等验证码",
    "done": "已完成",
    "error": "出错",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    filename          TEXT NOT NULL,
    source_path       TEXT NOT NULL,
    stored_path       TEXT,
    file_size         INTEGER DEFAULT 0,
    file_hash         TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',

    -- 抽取出来的发票字段
    invoice_code      TEXT,
    invoice_number    TEXT,
    invoice_date      TEXT,
    check_code        TEXT,
    check_code_last6  TEXT,
    amount            TEXT,
    seller_name       TEXT,
    buyer_name        TEXT,
    invoice_kind      TEXT,
    extract_source    TEXT,
    extract_notes     TEXT,        -- JSON 数组

    -- 查验结果
    verify_status     TEXT,
    verify_label      TEXT,
    verify_summary    TEXT,
    verify_detail     TEXT,
    captcha_source    TEXT,
    captcha_attempts  INTEGER DEFAULT 0,

    -- 人工验证码
    captcha_png       BLOB,
    captcha_at        REAL,

    -- 元信息
    attempts          INTEGER DEFAULT 0,
    error             TEXT,
    note              TEXT,
    manual_fields     INTEGER DEFAULT 0,   -- 是否人工补录过
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    verified_at       REAL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_hash    ON tasks(file_hash);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# 允许被 update_task 修改的列（白名单，防止列名注入）
_UPDATABLE = frozenset({
    "filename", "source_path", "stored_path", "file_size", "file_hash", "status",
    "invoice_code", "invoice_number", "invoice_date", "check_code", "check_code_last6",
    "amount", "seller_name", "buyer_name", "invoice_kind", "extract_source", "extract_notes",
    "verify_status", "verify_label", "verify_summary", "verify_detail",
    "captcha_source", "captcha_attempts",
    "captcha_png", "captcha_at",
    "attempts", "error", "note", "manual_fields",
    "created_at", "updated_at", "verified_at",
})


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 基础设施 -----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as exc:
                log.debug("关闭数据库失败（忽略）：%s", exc)

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def _query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # -- 写入 ---------------------------------------------------------------
    def add_task(
        self,
        *,
        filename: str,
        source_path: str,
        file_size: int = 0,
        file_hash: str | None = None,
        task_id: str | None = None,
        status: str = "pending",
    ) -> str:
        tid = task_id or uuid.uuid4().hex[:16]
        now = time.time()
        self._execute(
            """INSERT OR IGNORE INTO tasks
               (id, filename, source_path, file_size, file_hash, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, filename, source_path, file_size, file_hash, status, now, now),
        )
        return tid

    def update_task(self, task_id: str, **fields: Any) -> None:
        payload = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if not payload:
            return

        # 列表/字典自动转 JSON 文本
        for key, value in list(payload.items()):
            if isinstance(value, (list, dict)):
                payload[key] = json.dumps(value, ensure_ascii=False)

        payload["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in payload)
        self._execute(
            f"UPDATE tasks SET {cols} WHERE id = ?",
            (*payload.values(), task_id),
        )

    def set_status(self, task_id: str, status: str, **fields: Any) -> None:
        self.update_task(task_id, status=status, **fields)

    def set_captcha(self, task_id: str, png: bytes | None) -> None:
        self.update_task(task_id, captcha_png=png, captcha_at=time.time() if png else None)

    # -- 读取 ---------------------------------------------------------------
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def find_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM tasks WHERE file_hash = ? ORDER BY created_at DESC LIMIT 1",
            (file_hash,),
        )

    def find_by_path(self, source_path: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM tasks WHERE source_path = ? ORDER BY created_at DESC LIMIT 1",
            (source_path,),
        )

    def list_tasks(
        self,
        *,
        status: str | None = None,
        verify_status: str | None = None,
        limit: int = 200,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if verify_status:
            where.append("verify_status = ?")
            params.append(verify_status)

        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at " + ("DESC" if newest_first else "ASC")
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        return self._query(sql, params)

    def next_queued(self, statuses: tuple[str, ...] = ("pending", "ready")) -> dict[str, Any] | None:
        """取下一个待处理任务（先进先出）。"""
        placeholders = ",".join("?" for _ in statuses)
        return self._query_one(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
            f"ORDER BY created_at ASC LIMIT 1",
            statuses,
        )

    def count_by_status(self) -> dict[str, int]:
        rows = self._query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        out = {s: 0 for s in STATUSES}
        for row in rows:
            out[row["status"]] = row["n"]
        return out

    def count_by_verify_status(self) -> dict[str, int]:
        rows = self._query(
            "SELECT verify_status, COUNT(*) AS n FROM tasks "
            "WHERE verify_status IS NOT NULL GROUP BY verify_status"
        )
        return {row["verify_status"]: row["n"] for row in rows}

    def total(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM tasks")
        return int(row["n"]) if row else 0

    # -- 维护 ---------------------------------------------------------------
    def reset_in_flight(self) -> int:
        """进程重启后把「正在做」的任务退回可处理状态。

        返回受影响的行数。没有这一步，任务会永远卡在「查验中」。
        """
        placeholders = ",".join("?" for _ in IN_FLIGHT)
        cur = self._execute(
            f"""UPDATE tasks
                SET status = CASE WHEN invoice_number IS NULL THEN 'pending' ELSE 'ready' END,
                    error = COALESCE(error, '') || ' [进程重启，已重置]',
                    captcha_png = NULL,
                    updated_at = ?
                WHERE status IN ({placeholders})""",
            (time.time(), *IN_FLIGHT),
        )
        n = cur.rowcount or 0
        if n:
            log.info("重置了 %d 个因进程重启而卡住的任务", n)
        return n

    def delete_task(self, task_id: str) -> None:
        self._execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def clear_all(self) -> None:
        self._execute("DELETE FROM tasks")
