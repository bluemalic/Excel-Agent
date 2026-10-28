"""pandas 计算沙箱 runner（读工具 compute 的执行器；与 SqlRunner 同一套超时/重建策略）。

仅作复杂变换的降级工具：import 白名单 + 子进程隔离 + 5s 超时击杀。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass

from src import config


@dataclass
class CodeResult:
    columns: list[str]
    rows: list[list]
    total_rows: int


class CodeSandboxError(RuntimeError):
    pass


class CodeRunner:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._io_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "CodeRunner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _spawn(self) -> None:
        import os

        self._kill()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "src.sandbox.code_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=str(config.PROJECT_ROOT), env=env,
        )
        line = self._proc.stdout.readline()
        if not line or "ready" not in line:
            self._kill()
            raise CodeSandboxError("计算沙箱子进程启动失败")

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def run(self, code: str, tables: dict[str, str], max_rows: int | None = None) -> CodeResult:
        """执行受限 pandas 代码；代码需把结果赋给变量 result。"""
        # 基本静态拦截（与 import 白名单双保险）
        lowered = code.lower()
        for kw in ("open(", "__import__", "subprocess", "os.system", "eval(", "exec(",
                   "socket", "requests", "urllib", "shutil", "pathlib"):
            if kw in lowered:
                raise CodeSandboxError(f"计算沙箱禁止使用「{kw}」")
        with self._io_lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            self._req_id += 1
            req = {"id": self._req_id, "code": code, "tables": tables,
                   "max_rows": max_rows or config.SQL_MAX_ROWS}
            return self._execute(req, retry=True)

    def _execute(self, req: dict, retry: bool) -> CodeResult:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            if retry:
                self._spawn()
                return self._execute(req, retry=False)
            raise CodeSandboxError("计算沙箱连接中断")

        timer = threading.Timer(config.SQL_TIMEOUT_SECONDS, self._kill)
        timer.start()
        try:
            line = self._proc.stdout.readline()
        finally:
            timer.cancel()
        if not line:
            self._kill()
            if retry:
                self._spawn()
                return self._execute(req, retry=False)
            raise CodeSandboxError(f"计算超时（>{config.SQL_TIMEOUT_SECONDS}s）或沙箱崩溃")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            self._kill()
            raise CodeSandboxError("计算沙箱返回异常数据")
        if not resp.get("ok"):
            raise CodeSandboxError(str(resp.get("error", "未知错误")))
        return CodeResult(columns=resp["columns"], rows=resp["rows"],
                          total_rows=resp["total_rows"])

    def shutdown(self) -> None:
        with self._io_lock:
            self._kill()
