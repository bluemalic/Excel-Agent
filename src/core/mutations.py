"""结构化写操作的纯函数执行器（规格 §3.2：写工具不执行模型生成的自由代码）。

每个执行器签名统一：
    fn(df: DataFrame, params: dict) -> MutationOutcome(new_df, inverse, diff)

- inverse 是逆操作描述（同注册表的另一个 op），构成逆操作日志的核心；
- diff 是执行前的变更预览（用户确认的就是它）；
- 非法参数抛 OpError（列名不存在/行号越界/类型不支持），上层回喂模型重试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from src.core.diff import CellChange, DiffResult

# pandas 可空类型映射（parquet 友好，且能保留 NaN）
_CAST_MAP = {
    "int": "Int64", "float": "Float64", "string": "string",
    "datetime": "datetime64[ns]", "bool": "boolean",
}


class OpError(ValueError):
    """写操作参数非法（中文错误信息直接回喂模型/用户）。"""


@dataclass
class MutationOutcome:
    new_df: pd.DataFrame
    inverse: dict                                    # {"op_type": ..., "params": ...}
    diff: DiffResult
    style_writes: dict[str, dict | None] = field(default_factory=dict)  # 本操作对样式表的改动


def _col(df: pd.DataFrame, name: str) -> str:
    """校验列存在，返回列名（同时兼容数字下标输入）。"""
    if name in df.columns:
        return name
    if isinstance(name, int) and 0 <= name < len(df.columns):
        return str(df.columns[name])
    raise OpError(f"列「{name}」不存在。现有列：{list(df.columns)}")


def _row(df: pd.DataFrame, idx: int, what: str = "行号") -> int:
    """校验行号在数据范围内（0-based）。"""
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise OpError(f"{what}必须是整数，收到 {idx!r}")
    if idx < 0 or idx >= len(df):
        raise OpError(f"{what} {idx} 越界：当前共 {len(df)} 行数据（0-based）")
    return idx


def _coerce_value(series: pd.Series, value) -> object:
    """把外部传入的值按列类型收敛（数字列收数字、日期列解析日期，失败保持原样并在上层提示）。"""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if v == "":
            return None
        if pd.api.types.is_string_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series):
            return v
        # 数值列
        num = v.replace(",", "").replace("￥", "").replace("¥", "")
        try:
            f = float(num)
            if "Int" in str(series.dtype):
                return int(f) if f == int(f) else f  # 非整数塞进 Int 列会被 pandas 拒绝，保留 float 提示用户
            return f
        except ValueError:
            pass
        if pd.api.types.is_datetime64_any_dtype(series):
            try:
                return pd.to_datetime(v)
            except (ValueError, TypeError):
                pass
        return v
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _cells_diff(df_old: pd.DataFrame, df_new: pd.DataFrame, op_type: str, sheet: str,
                description: str, limit: int = 400) -> DiffResult:
    """逐格比较新旧表，生成通用 DiffResult（供整表型操作兜底使用）。"""
    d = DiffResult(op_type=op_type, sheet=sheet, description=description)
    common = [c for c in df_new.columns if c in df_old.columns]
    n = max(len(df_old), len(df_new))
    for i in range(n):
        for c in common:
            old = df_old[c].iloc[i] if i < len(df_old) else None
            new = df_new[c].iloc[i] if i < len(df_new) else None
            old_na = pd.isna(old) if not isinstance(old, (list, tuple)) else False
            new_na = pd.isna(new) if not isinstance(new, (list, tuple)) else False
            if old_na != new_na or (not old_na and not new_na and old != new):
                d.changes.append(CellChange(f"第 {i + 1} 行", str(c), str(old), str(new)))
    d.total_changes = len(d.changes)
    d.n_cells = d.total_changes
    d.changes = d.changes[:limit]
    d.rows_added = max(0, len(df_new) - len(df_old))
    d.rows_removed = max(0, len(df_old) - len(df_new))
    return d


# ---------------------------------------------------------------------------
# 各操作执行器
# ---------------------------------------------------------------------------
def op_set_cell(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """set_cell / set_formula：改单个单元格。params: {row, column, value}"""
    col = _col(df, params["column"])
    row = _row(df, int(params["row"]))
    value = _coerce_value(df[col], params.get("value"))
    new_df = df.copy()
    old = df.at[df.index[row], col]
    new_df.at[new_df.index[row], col] = value
    d = DiffResult(op_type="set_cell", sheet=params["sheet"],
                   description=params.get("description") or
                   f"将第 {row + 1} 行「{col}」由 {_val(old)} 改为 {_val(value)}")
    d.changes = [CellChange(f"第 {row + 1} 行", col, _val(old), _val(value))]
    d.total_changes = d.n_cells = 1
    inverse = {"op_type": "set_cells", "params": {
        "sheet": params["sheet"],
        "cells": [{"row": row, "column": col, "value": None if pd.isna(old) else old}]}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def _val(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or (not isinstance(v, (list, tuple)) and pd.isna(v)):
        return "（空）"
    return str(v)


def op_set_cells(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """set_cells：批量设置多个单元格（逆向恢复专用）。params: {cells: [{row, column, value}]}"""
    new_df = df.copy()
    changes: list[CellChange] = []
    olds: list[dict] = []
    for c in params["cells"]:
        col = _col(df, c["column"])
        row = _row(df, int(c["row"]))
        value = _coerce_value(df[col], c.get("value"))
        old = df.at[df.index[row], col]
        new_df.at[new_df.index[row], col] = value
        changes.append(CellChange(f"第 {row + 1} 行", col, _val(old), _val(value)))
        olds.append({"row": row, "column": col,
                     "value": None if pd.isna(old) else old})
    d = DiffResult(op_type="set_cells", sheet=params["sheet"],
                   description=params.get("description") or f"批量修改 {len(changes)} 个单元格")
    d.changes = changes[:400]
    d.total_changes = d.n_cells = len(changes)
    inverse = {"op_type": "set_cells", "params": {"sheet": params["sheet"], "cells": olds}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_set_range(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """set_range：矩形区域写入。params: {start_row, columns: [名], values: [[..],[..]]}"""
    columns = [_col(df, c) for c in params["columns"]]
    start_row = _row(df, int(params["start_row"]), "起始行号")
    values = params["values"]
    if len(values) > len(df) - start_row:
        raise OpError(f"写入区域超出表尾：起始行 {start_row}，写入 {len(values)} 行，表共 {len(df)} 行")
    for r in values:
        if len(r) != len(columns):
            raise OpError("values 每行的值个数必须与 columns 数一致")
    new_df = df.copy()
    changes: list[CellChange] = []
    olds: list[dict] = []
    for i, row_vals in enumerate(values):
        r = start_row + i
        for j, col in enumerate(columns):
            value = _coerce_value(df[col], row_vals[j])
            old = df.at[df.index[r], col]
            new_df.at[new_df.index[r], col] = value
            changes.append(CellChange(f"第 {r + 1} 行", col, _val(old), _val(value)))
            olds.append({"row": r, "column": col, "value": None if pd.isna(old) else old})
    d = DiffResult(op_type="set_range", sheet=params["sheet"],
                   description=params.get("description") or
                   f"区域写入：第 {start_row + 1} 行起 × {len(columns)} 列 × {len(values)} 行")
    d.changes = changes[:400]
    d.total_changes = d.n_cells = len(changes)
    inverse = {"op_type": "set_cells", "params": {"sheet": params["sheet"], "cells": olds}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_add_rows(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """add_rows：表尾/指定位置追加行。params: {rows: [{列名: 值}], at_row?: 插入位置}"""
    rows = params.get("rows") or []
    if not rows:
        raise OpError("add_rows 需要至少一行数据")
    at_row = params.get("at_row")
    if at_row is None or int(at_row) >= len(df):
        new_df = pd.concat([df, pd.DataFrame(rows, columns=df.columns)], ignore_index=True)
        pos_desc = "表尾"
    else:
        at = _row(df, int(at_row), "插入位置")
        top = df.iloc[:at]
        bottom = df.iloc[at:]
        new_df = pd.concat([top, pd.DataFrame(rows, columns=df.columns), bottom], ignore_index=True)
        pos_desc = f"第 {at + 1} 行前"
    # 对齐列 dtype（pandas 3 无 astype(errors="ignore")，改用逐列安全转换）
    for col in df.columns:
        if new_df[col].dtype != df[col].dtype:
            try:
                new_df[col] = new_df[col].astype(df[col].dtype)
            except (TypeError, ValueError):
                pass
    d = DiffResult(op_type="add_rows", sheet=params["sheet"],
                   description=params.get("description") or f"在{pos_desc}追加 {len(rows)} 行")
    d.rows_added = len(rows)
    d.total_changes = d.n_cells = len(rows) * len(df.columns)
    d.changes = [CellChange(f"新行 {i + 1}", str(k), "（新增）", _val(v))
                 for i, row in enumerate(rows) for k, v in list(row.items())[:6]]
    inverse = {"op_type": "delete_rows", "params": {
        "sheet": params["sheet"],
        "start_row": len(df) if at_row is None else int(at_row),
        "count": len(rows)}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_delete_rows(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """delete_rows：删除连续若干行。params: {start_row, count}"""
    start = _row(df, int(params["start_row"]), "起始行号")
    count = int(params.get("count", 1))
    if count <= 0 or start + count > len(df):
        raise OpError(f"删除范围越界：起始 {start}，删除 {count} 行，表共 {len(df)} 行")
    removed = df.iloc[start:start + count]
    new_df = pd.concat([df.iloc[:start], df.iloc[start + count:]], ignore_index=True)
    for col in df.columns:
        if new_df[col].dtype != df[col].dtype:
            try:
                new_df[col] = new_df[col].astype(df[col].dtype)
            except (TypeError, ValueError):
                pass
    d = DiffResult(op_type="delete_rows", sheet=params["sheet"],
                   description=params.get("description") or f"删除第 {start + 1} 至 {start + count} 行（共 {count} 行）")
    d.rows_removed = count
    d.total_changes = d.n_cells = count * len(df.columns)
    d.changes = [
        CellChange(f"第 {start + i + 1} 行", "（整行）",
                   "、".join(f"{c}={_val(v)}" for c, v in list(row.items())[:4]), "（删除）")
        for i, (_, row) in enumerate(removed.iterrows()) if i < 30
    ]
    rows_back = [{c: (None if pd.isna(v) else v) for c, v in row.items()} for _, row in removed.iterrows()]
    inverse = {"op_type": "add_rows", "params": {
        "sheet": params["sheet"], "rows": rows_back, "at_row": start}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_add_column(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """add_column：新增列（values 显式给出，或由沙箱表达式预先算好传入）。params: {name, values: [..]}"""
    name = str(params["name"]).strip()
    if not name:
        raise OpError("新列名不能为空")
    if name in df.columns:
        raise OpError(f"列「{name}」已存在")
    values = params.get("values")
    if values is None:
        values = [None] * len(df)
    if len(values) != len(df):
        raise OpError(f"新列值数量（{len(values)}）与行数（{len(df)}）不一致")
    new_df = df.copy()
    new_df[name] = pd.array(list(values), dtype="Float64") if _all_numeric(values) else list(values)
    sample = [f"第 {i + 1} 行 → {_val(v)}" for i, v in enumerate(values[:6])]
    d = DiffResult(op_type="add_column", sheet=params["sheet"],
                   description=params.get("description") or f"新增列「{name}」")
    d.total_changes = d.n_cells = len(values)
    d.extra["新列示例"] = sample
    inverse = {"op_type": "drop_column", "params": {"sheet": params["sheet"], "column": name}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def _all_numeric(values) -> bool:
    ok = 0
    for v in values:
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ok += 1
        else:
            return False
    return ok > 0


def op_restore_column(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """restore_column：把某列整体恢复为记录的旧值（cast_column / drop_column 的逆操作）。"""
    col = _col(df, params["column"])
    values = params["values"]
    if len(values) != len(df):
        raise OpError(f"恢复值数量（{len(values)}）与当前行数（{len(df)}）不一致，无法恢复")
    new_df = df.copy()
    new_df[col] = values
    d = DiffResult(op_type="restore_column", sheet=params["sheet"],
                   description=f"恢复列「{col}」为操作前内容")
    d.total_changes = d.n_cells = len(values)
    d.changes = []
    inverse = {"op_type": "noop", "params": {"sheet": params["sheet"]}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_rename_column(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """rename_column。params: {old, new}"""
    old = _col(df, params["old"])
    new = str(params["new"]).strip()
    if not new:
        raise OpError("新列名不能为空")
    if new in df.columns and new != old:
        raise OpError(f"列「{new}」已存在，不能重名")
    new_df = df.copy()
    new_df = new_df.rename(columns={old: new})
    d = DiffResult(op_type="rename_column", sheet=params["sheet"],
                   description=params.get("description") or f"列「{old}」重命名为「{new}」")
    d.n_cells = 0
    inverse = {"op_type": "rename_column", "params": {"sheet": params["sheet"], "old": new, "new": old}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_drop_column(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """drop_column：删除整列（高风险：触发确认）。params: {column}"""
    col = _col(df, params["column"])
    old_values = [None if pd.isna(v) else v for v in df[col].tolist()]
    new_df = df.drop(columns=[col])
    d = DiffResult(op_type="drop_column", sheet=params["sheet"],
                   description=params.get("description") or f"删除列「{col}」（{len(old_values)} 行数据）")
    d.rows_removed = 0
    d.total_changes = d.n_cells = len(old_values)
    d.extra["被删列样本"] = [str(v) for v in old_values[:3]]
    inverse = {"op_type": "restore_column", "params": {
        "sheet": params["sheet"], "column": col, "values": old_values}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_fill_missing(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """fill_missing：缺失值填充。params: {column, strategy: mean|median|mode|ffill|value, value?}"""
    col = _col(df, params["column"])
    strategy = params.get("strategy", "value")
    series = df[col]
    mask = series.isna()
    if not mask.any():
        raise OpError(f"列「{col}」没有缺失值，无需填充")
    if strategy in ("mean", "median"):
        if not pd.api.types.is_numeric_dtype(series):
            raise OpError(f"列「{col}」不是数值列，不能做 {strategy} 填充（可改用 value/mode/ffill）")
        fill_v = series.mean() if strategy == "mean" else series.median()
    elif strategy == "mode":
        mode = series.mode(dropna=True)
        if mode.empty:
            raise OpError(f"列「{col}」没有可用众数")
        fill_v = mode.iloc[0]
    elif strategy == "ffill":
        fill_v = None  # 逐行前向填充，特殊处理
    elif strategy == "value":
        fill_v = _coerce_value(series, params.get("value"))
        if fill_v is None:
            raise OpError("value 填充策略需要提供非空 value 参数")
    else:
        raise OpError(f"不支持的填充策略：{strategy}（可选 mean/median/mode/ffill/value）")

    new_df = df.copy()
    if strategy == "ffill":
        new_df[col] = new_df[col].ffill()
        # 前向填充后仍为空的（首行即空）保持缺失
    else:
        new_df.loc[mask, col] = fill_v

    filled_idx = mask[mask].index
    changes = [CellChange(f"第 {df.index.get_loc(i) + 1} 行", col, "（空）", _val(new_df.at[i, col]))
               for i in filled_idx[:400]]
    d = DiffResult(op_type="fill_missing", sheet=params["sheet"],
                   description=params.get("description") or
                   f"列「{col}」缺失值填充：策略 {strategy}"
                   + (f"，填充值 {fill_v}" if strategy != "ffill" else "，前向填充"))
    d.changes = changes
    d.total_changes = d.n_cells = int(mask.sum())
    cells = [{"row": df.index.get_loc(i), "column": col, "value": None} for i in filled_idx]
    inverse = {"op_type": "set_cells", "params": {"sheet": params["sheet"], "cells": cells}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_drop_duplicates(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """drop_duplicates：删重复行。params: {subset?: [列], keep: first|last}"""
    subset = params.get("subset")
    if subset:
        subset = [_col(df, c) for c in subset]
    keep = params.get("keep", "first")
    if keep not in ("first", "last"):
        raise OpError(f"keep 只支持 first/last，收到 {keep}")
    dup_mask = df.duplicated(subset=subset, keep=keep)
    if not dup_mask.any():
        raise OpError("没有发现重复行")
    removed = df[dup_mask]
    new_df = df[~dup_mask].reset_index(drop=True)
    d = DiffResult(op_type="drop_duplicates", sheet=params["sheet"],
                   description=params.get("description") or
                   f"删除重复行 {int(dup_mask.sum())} 行"
                   + (f"（判定列：{'、'.join(subset)}）" if subset else "（按整行判定）"))
    d.rows_removed = int(dup_mask.sum())
    d.total_changes = d.n_cells = d.rows_removed * len(df.columns)
    d.changes = [
        CellChange(f"第 {df.index.get_loc(i) + 1} 行", "（整行）", "、".join(f"{c}={_val(v)}" for c, v in list(r.items())[:4]), "（删除）")
        for i, (_, r) in enumerate(removed.iterrows()) if i < 30
    ]
    rows_back = [{c: (None if pd.isna(v) else v) for c, v in row.items()} for _, row in removed.iterrows()]
    inverse = {"op_type": "add_rows", "params": {
        "sheet": params["sheet"], "rows": rows_back,
        "at_row": int(df.index.get_loc(removed.index[0])) if len(removed) else 0}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_cast_column(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """cast_column：列类型转换。params: {column, dtype: int|float|string|datetime|bool, on_error: fail|coerce}"""
    col = _col(df, params["column"])
    dtype = params.get("dtype")
    if dtype not in _CAST_MAP:
        raise OpError(f"不支持的目标类型：{dtype}（可选 {'/'.join(_CAST_MAP)}）")
    on_error = params.get("on_error", "coerce")
    old_values = [None if pd.isna(v) else v for v in df[col].tolist()]
    target = _CAST_MAP[dtype]
    try:
        if dtype == "datetime":
            new_series = pd.to_datetime(df[col], errors="raise")
        else:
            new_series = df[col].astype(target)
    except (ValueError, TypeError) as e:
        if on_error != "coerce":
            raise OpError(f"列「{col}」转换为 {dtype} 失败：{e}（可让用户确认后用 coerce 忽略错误值置空）")
        if dtype == "datetime":
            new_series = pd.to_datetime(df[col], errors="coerce")
        elif dtype == "int":
            new_series = pd.to_numeric(df[col], errors="coerce").round().astype("Int64")
        elif dtype == "float":
            new_series = pd.to_numeric(df[col], errors="coerce").astype("Float64")
        else:
            new_series = df[col].astype(target)
    new_df = df.copy()
    new_df[col] = new_series

    def _norm(v) -> str:
        if v is None or (not isinstance(v, (list, tuple)) and pd.isna(v)):
            return "∅"
        return str(v)

    old_norm = [_norm(v) for v in old_values]
    new_norm = [_norm(v) for v in new_series.tolist()]
    n_non_null = int(df[col].notna().sum())
    # 类型转换的影响面按"非空单元格全部受影响"计（即使字符串表示相同，类型也变了）
    d = DiffResult(op_type="cast_column", sheet=params["sheet"],
                   description=params.get("description") or
                   f"列「{col}」类型由 {df[col].dtype} 转换为 {dtype}（{n_non_null} 个非空值）")
    d.total_changes = d.n_cells = n_non_null
    sample_pairs = [(o, n) for o, n in zip(old_norm, new_norm) if o != n][:6]
    d.changes = [CellChange("示例", col, o if o != "∅" else "（空）", n if n != "∅" else "（空）")
                 for o, n in sample_pairs]
    d.extra["转换说明"] = f"{n_non_null}/{len(old_values)} 个非空单元格转换为 {dtype}"
    inverse = {"op_type": "restore_column", "params": {
        "sheet": params["sheet"], "column": col, "values": old_values}}
    return MutationOutcome(new_df=new_df, inverse=inverse, diff=d)


def op_noop(df: pd.DataFrame, params: dict) -> MutationOutcome:
    d = DiffResult(op_type="noop", sheet=params.get("sheet", ""), description="无数据变化")
    return MutationOutcome(new_df=df.copy(), inverse={"op_type": "noop", "params": params}, diff=d)


def op_create_sheet(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """create_sheet：新建工作表（初始内容即 df；新建工作表的 df 由调用方构造）。

    本执行器不修改现有 df；新建 sheet 的数据由 workbook.apply_op 特殊分支处理。
    params 仅需 {sheet_name}。
    """
    d = DiffResult(op_type="create_sheet", sheet=params["sheet_name"],
                   description=params.get("description") or f"新建工作表「{params['sheet_name']}」（{len(df)} 行 × {len(df.columns)} 列）")
    d.rows_added = len(df)
    d.total_changes = d.n_cells = len(df) * max(len(df.columns), 1)
    inverse = {"op_type": "drop_sheet", "params": {"sheet": params["sheet_name"]}}
    return MutationOutcome(new_df=df, inverse=inverse, diff=d)


def op_drop_sheet(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """drop_sheet：撤销时停用 sheet（数据保留在磁盘，由版本重放逻辑处理激活位）。"""
    d = DiffResult(op_type="drop_sheet", sheet=params.get("sheet", ""), description="停用工作表")
    return MutationOutcome(new_df=df, inverse={"op_type": "noop", "params": params}, diff=d)


def op_style_cells(df: pd.DataFrame, params: dict) -> MutationOutcome:
    """style_cells：设置单元格/区域/整列样式（不改数据，只改样式表）。

    params: {targets: ['A1', 'A1:C10', 'col:日期'], style: {bold?, font_color?, bg_color?, number_format?, width?}}
    """
    targets = params.get("targets") or []
    style = params.get("style") or {}
    if not targets:
        raise OpError("style_cells 需要至少一个目标（如 'B2'、'A1:C10'、'col:销售额'）")
    d = DiffResult(op_type="style_cells", sheet=params["sheet"],
                   description=params.get("description") or f"设置样式：{'、'.join(targets)}")
    d.style_changes = {t: {"old": "（执行前查询）", "new": str(style)} for t in targets}
    d.n_cells = len(targets)
    d.total_changes = len(targets)
    inverse = {"op_type": "style_restore", "params": {"sheet": params["sheet"], "targets": targets}}
    return MutationOutcome(new_df=df.copy(), inverse=inverse, diff=d)


def op_style_restore(df: pd.DataFrame, params: dict) -> MutationOutcome:
    d = DiffResult(op_type="style_restore", sheet=params.get("sheet", ""), description="恢复原样式")
    return MutationOutcome(new_df=df.copy(), inverse={"op_type": "noop", "params": params}, diff=d)


# 执行器注册表（LLM 只能调用这里登记过的结构化操作）
EXECUTORS: dict[str, callable] = {
    "set_cell": op_set_cell,
    "set_cells": op_set_cells,
    "set_range": op_set_range,
    "add_rows": op_add_rows,
    "delete_rows": op_delete_rows,
    "add_column": op_add_column,
    "restore_column": op_restore_column,
    "rename_column": op_rename_column,
    "drop_column": op_drop_column,
    "fill_missing": op_fill_missing,
    "drop_duplicates": op_drop_duplicates,
    "cast_column": op_cast_column,
    "create_sheet": op_create_sheet,
    "drop_sheet": op_drop_sheet,
    "style_cells": op_style_cells,
    "style_restore": op_style_restore,
    "noop": op_noop,
}

# 高风险操作（触发 diff 确认的额外条件之一，规格 §3.3）
HIGH_RISK_OPS = {"delete_rows", "drop_column", "drop_duplicates", "create_sheet"}
