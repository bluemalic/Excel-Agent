"""SQL 沙箱 runner：管理 worker 子进程生命周期 + 超时击杀 + guard 前置校验。

- 校验（sqlguard）：语句类型 / 表白名单 / 列白名单，失败抛 SqlGuardError；
- 执行（子进程）：持久 worker 摊薄启动成本；单条 SQL 超时 5s → 杀进程重建；
- worker 意外退出：自动重建并报错，会话不中断（AC-9）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass

from src import config
from src.sandbox.sqlguard import SqlGuardError, ensure_limit, validate_sql


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    total_rows: int
    elapsed_ms: int
    truncated: bool
    sql_executed: str


class SandboxTimeoutError(TimeoutError):
    pass


class SandboxCrashError(RuntimeError):
    pass


class SqlRunner:
    """进程级单例。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._io_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "SqlRunner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    def _spawn(self) -> subprocess.Popen:
        import os

        self._kill()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "src.sandbox.sql_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=str(config.PROJECT_ROOT), env=env,
        )
        # 等待 ready 信号
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line or "ready" not in line:
            self._kill()
            raise SandboxCrashError("SQL 沙箱子进程启动失败")

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    def query(self, sql: str, tables: dict[str, str],
              table_columns: dict[str, list[str]] | None = None,
              max_rows: int | None = None) -> QueryResult:
        """执行一条只读查询。

        tables: {视图名: parquet 路径}；table_columns: {视图名: 列名列表}（校验用，
        不传时用视图名占位——但列级白名单会失效，正式路径务必传）。
        """
        cols = table_columns or {name: [] for name in tables}
        guard = validate_sql(sql, cols)
        if not guard.ok:
            raise SqlGuardError(guard.message)
        safe_sql = ensure_limit(sql, max_rows or config.SQL_MAX_ROWS)

        with self._io_lock:
            if not self._alive():
                self._spawn()
            self._req_id += 1
            req = {"id": self._req_id, "sql": safe_sql, "tables": tables,
                   "max_rows": max_rows or config.SQL_MAX_ROWS}
            return self._execute(req, safe_sql, retry=True)

    def _execute(self, req: dict, safe_sql: str, retry: bool) -> QueryResult:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            if retry:
                self._spawn()
                return self._execute(req, safe_sql, retry=False)
            raise SandboxCrashError("SQL 沙箱连接中断")

        timer = threading.Timer(config.SQL_TIMEOUT_SECONDS, self._kill)
        timer.start()
        try:
            line = self._proc.stdout.readline()
        finally:
            timer.cancel()

        if not line:  # 超时被杀或崩溃
            self._kill()
            if retry:
                self._spawn()
                return self._execute(req, safe_sql, retry=False)
            raise SandboxTimeoutError(
                f"查询执行超过 {config.SQL_TIMEOUT_SECONDS} 秒已中止，请简化 SQL（如加过滤/减少聚合列）")

        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            self._kill()
            if retry:
                self._spawn()
                return self._execute(req, safe_sql, retry=False)
            raise SandboxCrashError("SQL 沙箱返回异常数据")

        if not resp.get("ok"):
            err = str(resp.get("error", "未知错误"))
            if retry and ("IO Error" in err or "CRASHED" in err.upper()):
                self._spawn()
                return self._execute(req, safe_sql, retry=False)
            raise SqlGuardError(err)
        return QueryResult(
            columns=resp["columns"], rows=resp["rows"], total_rows=resp["total_rows"],
            elapsed_ms=resp.get("elapsed_ms", 0), truncated=resp.get("truncated", False),
            sql_executed=safe_sql,
        )

    def warmup(self) -> None:
        """应用启动时预热沙箱子进程（导入 duckdb 约 1s，避免首次查询计入）。"""
        with self._io_lock:
            if not self._alive():
                self._spawn()

    def shutdown(self) -> None:
        with self._io_lock:
            self._kill()
