"""pandas 计算沙箱子进程（读工具 compute 的降级执行环境，规格 §3.1/§4）。

`python -m src.sandbox.code_worker`
请求: {"id": 1, "code": "...", "tables": {"明细": "path.parquet"}, "max_rows": 5000}
响应: {"id": 1, "ok": true, "columns": [...], "rows": [...], "total_rows": n}
      {"id": 1, "ok": false, "error": "..."}

安全措施：
- import 白名单：pandas/numpy/math/datetime/re/json/statistics；
- 内建函数白名单：无 open/exec/eval/compile/__import__ 原生入口；
- 子进程隔离 + 父进程 5s 超时击杀（与 SQL worker 同一套 runner 思路）。
"""

from __future__ import annotations

import json
import math
import sys

import numpy as np

ALLOWED_MODULES = {"pandas", "numpy", "math", "datetime", "re", "json", "statistics"}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    root = name.split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(f"沙箱内禁止 import「{name}」（允许：{sorted(ALLOWED_MODULES)}）")
    return __import__(name, globals, locals, fromlist, level)


def _clean_value(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ", timespec="seconds") if hasattr(v, "hour") else v.isoformat()
    if hasattr(v, "item"):
        return _clean_value(v.item())
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def main() -> None:
    import builtins
    import pandas as pd

    safe_builtins = {k: v for k, v in vars(builtins).items()
                     if k not in ("open", "exec", "eval", "compile", "__import__",
                                  "input", "breakpoint", "exit", "quit", "compile")}
    env = {
        "__builtins__": {**safe_builtins, "__import__": _guarded_import},
        "pd": pd,
    }

    _emit({"event": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _emit({"id": None, "ok": False, "error": "请求不是合法 JSON"})
            continue
        rid = req.get("id")
        try:
            # 每个请求注册本次的表变量（df 名称 = 视图名）
            local_env = dict(env)
            for name, path in (req.get("tables") or {}).items():
                if not str(name).isidentifier():
                    local_env[f"t_{len(local_env)}"] = pd.read_parquet(path)
                else:
                    local_env[name] = pd.read_parquet(path)
            code = req["code"]
            max_rows = int(req.get("max_rows", 5000))
            exec(code, {"__builtins__": local_env["__builtins__"]}, local_env)  # noqa: S102 —— 沙箱受限环境
            result = local_env.get("result")
            if result is None:
                raise ValueError("代码必须把结果赋给变量 result（如 result = 明细.groupby(...)）")
            if isinstance(result, pd.DataFrame):
                total = len(result)
                out = result.head(max_rows)
                _emit({"id": rid, "ok": True, "columns": [str(c) for c in out.columns],
                       "rows": [[_clean_value(v) for v in row] for row in out.itertuples(index=False)],
                       "total_rows": total, "truncated": total > max_rows})
            elif isinstance(result, pd.Series):
                # Series（如 groupby().sum()）→ 索引+值 两列
                s = result
                total = len(s)
                head = s.head(max_rows)
                idx_name = str(s.index.name or "索引")
                _emit({"id": rid, "ok": True,
                       "columns": [idx_name, str(s.name or "值")],
                       "rows": [[_clean_value(i), _clean_value(v)] for i, v in head.items()],
                       "total_rows": total, "truncated": total > max_rows})
            elif isinstance(result, np.ndarray):
                total = int(result.shape[0]) if result.ndim else 1
                flat = result.reshape(-1)[:max_rows]
                _emit({"id": rid, "ok": True, "columns": ["值"],
                       "rows": [[_clean_value(v)] for v in flat],
                       "total_rows": total, "truncated": total > max_rows})
            elif isinstance(result, (list, tuple)) and result and isinstance(result[0], (list, tuple)):
                total = len(result)
                _emit({"id": rid, "ok": True, "columns": [f"列{i+1}" for i in range(len(result[0]))],
                       "rows": [[_clean_value(v) for v in r] for r in list(result)[:max_rows]],
                       "total_rows": total, "truncated": total > max_rows})
            else:
                _emit({"id": rid, "ok": True, "columns": ["结果"], "rows": [[_clean_value(result)]],
                       "total_rows": 1, "truncated": False})
        except Exception as e:  # noqa: BLE001
            _emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
