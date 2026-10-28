"""文件导入：xlsx / xls / csv → DataFrame（含脏表处理与编码识别）。

策略（规格 §2）：
- 表头自动探测：对前 6 行逐一打分（非空率、名称唯一性、该行字符串占比 vs 下方数据），
  取最优行；置信度低时返回候选行让用户手动指定，不硬猜。
- 合并单元格：openpyxl 读出合并区域，用左上角值向下/向右填充，并给出提示。
- CSV 编码：依次尝试 utf-8-sig → gb18030 → utf-16 → latin-1。
- 不静默转换类型：文本型数值/日期文本只在 schema 里标记，转换交给用户确认的写操作。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src import config


@dataclass
class ImportResult:
    """一次成功导入的产物。"""
    sheets: dict[str, pd.DataFrame]           # sheet 名 → DataFrame
    header_rows: dict[str, int]               # sheet 名 → 探测到的表头行号（0-based）
    warnings: list[str] = field(default_factory=list)   # 脏数据提示（进 UI 数据卡片）
    header_confidence: dict[str, float] = field(default_factory=dict)


class ImportTooLargeError(ValueError):
    pass


def check_file_size(path: str | Path) -> None:
    size = Path(path).stat().st_size
    if size > config.UPLOAD_MAX_BYTES:
        raise ImportTooLargeError(f"文件 {size / 1048576:.1f}MB 超过 20MB 上限")


# ---------------------------------------------------------------------------
# 表头探测
# ---------------------------------------------------------------------------
def _score_header_row(df_raw: pd.DataFrame, row_idx: int) -> float:
    """给候选表头行打分：非空率、唯一性、字符串占比、下方有数据。"""
    if row_idx >= len(df_raw):
        return -1.0
    row = df_raw.iloc[row_idx]
    cells = row.dropna().astype(str).str.strip()
    cells = cells[cells != ""]
    n_cells = len(cells)
    if n_cells < 2:
        return -1.0

    non_null_ratio = n_cells / len(row)
    unique_ratio = cells.nunique() / n_cells
    # 表头行通常以文本为主
    str_ratio = sum(1 for v in cells if not _looks_numeric(v)) / n_cells
    # 下方至少还要有一行数据
    has_data_below = row_idx < len(df_raw) - 1

    score = non_null_ratio * 0.4 + unique_ratio * 0.35 + str_ratio * 0.25
    if not has_data_below:
        score -= 0.5
    return score


def _looks_numeric(v: str) -> bool:
    v = str(v).replace(",", "").replace("￥", "").replace("¥", "")
    try:
        float(v)
        return True
    except ValueError:
        return False


def detect_header_row(df_raw: pd.DataFrame, max_scan: int = 6) -> tuple[int, float, list[int]]:
    """返回 (最优表头行号 0-based, 置信度 0~1, 候选行列表)。"""
    scores = {i: _score_header_row(df_raw, i) for i in range(min(max_scan, len(df_raw)))}
    valid = {i: s for i, s in scores.items() if s >= 0}
    if not valid:
        return 0, 0.0, []
    best = max(valid, key=valid.get)
    ranked = sorted(valid.values(), reverse=True)
    # 置信度 = 最优分与次优分的差距 + 绝对分
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else 1.0
    confidence = min(1.0, 0.5 * ranked[0] + 0.5 * margin + 0.2)
    candidates = [i for i, s in sorted(valid.items()) if s > 0]
    return best, confidence, candidates


def load_with_header(df_raw: pd.DataFrame, header_row: int,
                     from_csv: bool = False) -> pd.DataFrame:
    """以指定行作为表头重构 DataFrame，并清洗列名。

    from_csv: CSV 单元格无原生类型（读出来全是字符串），数值外形列直接转换；
    Excel 则保留原生类型，只有"整列都是数字外形的字符串"才保持文本并交由 schema 标记。
    """
    header = df_raw.iloc[header_row].tolist()
    data = df_raw.iloc[header_row + 1 :].reset_index(drop=True)
    # 列名清洗：去空白；空列名补"列N"；重复列名加后缀
    names: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        name = str(h).strip() if pd.notna(h) and str(h).strip() else f"列{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        names.append(name)
    data.columns = names
    # 全空行剔除，全空列保留（schema 里标记，由用户决定删不删）
    data = data.dropna(how="all").reset_index(drop=True)
    return _infer_object_columns(data, from_csv=from_csv)


def _infer_object_columns(df: pd.DataFrame, from_csv: bool = False) -> pd.DataFrame:
    """header=None 读取会让整列变 object；这里按原生值类型恢复数值/日期列。

    - 整列都是 Python 原生 int/float/datetime（Excel 数字单元格）→ 转换；
    - 整列都是数字外形的字符串：CSV 直接转换，Excel 视为"文本型数值"保持字符串；
    - 大部分可转但存在杂质（≥80% 可解析）→ 保持字符串，交给 schema 标记"文本型数值"。
    """
    import datetime as _dt

    for col in df.columns:
        s = df[col]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s)):
            continue
        non_null = s.dropna()
        if non_null.empty:
            continue
        values = list(non_null)
        if all(isinstance(v, bool) for v in values):
            df[col] = s.astype("boolean")
            continue
        native_num = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
        native_dt = all(isinstance(v, (_dt.datetime, _dt.date, pd.Timestamp)) for v in values)
        if native_num:
            df[col] = pd.to_numeric(s)
            continue
        if native_dt:
            try:
                df[col] = pd.to_datetime(s)
                continue
            except (ValueError, TypeError):
                pass
        parsed = pd.to_numeric(
            non_null.astype(str).str.replace(",", "").str.replace("￥", "").str.replace("¥", ""),
            errors="coerce",
        )
        ratio = float(parsed.notna().mean())
        if from_csv and ratio >= 0.8:
            df[col] = pd.to_numeric(
                s.astype(str).str.replace(",", "").str.replace("￥", "").str.replace("¥", ""),
                errors="coerce",
            )
        # 其余情况保持字符串：schema 嗅探负责标记"文本型数值/疑似日期文本"
    return df


# ---------------------------------------------------------------------------
# 合并单元格处理
# ---------------------------------------------------------------------------
def _fill_merged_cells(path: Path) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """读 xlsx 时把合并单元格的左上值填充到整个合并区域。"""
    from openpyxl import load_workbook

    warnings: list[str] = []
    wb = load_workbook(path, data_only=True, read_only=False)
    for ws in wb.worksheets:
        merged = list(ws.merged_cells.ranges)
        for rng in merged:
            top_left = ws.cell(rng.min_row, rng.min_col).value
            ws.unmerge_cells(str(rng))          # MergedCell 只读，先解除合并
            for row in ws.iter_rows(rng.min_row, rng.max_row, rng.min_col, rng.max_col):
                for cell in row:
                    cell.value = top_left
        if merged:
            warnings.append(f"工作表「{ws.title}」存在 {len(merged)} 处合并单元格，已按左上角值填充")
    # 整个工作簿一次性导出，再交给 pandas 按 sheet 读取
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    sheets = {
        name: df for name, df in
        pd.read_excel(io.BytesIO(buf.getvalue()), sheet_name=None, header=None).items()
    }
    return sheets, warnings


# ---------------------------------------------------------------------------
# CSV 编码识别
# ---------------------------------------------------------------------------
_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16", "latin-1")


def read_csv_auto(path: Path) -> tuple[pd.DataFrame, str, list[str]]:
    """自动识别编码读取 CSV，返回 (df, 实际编码, 警告)。"""
    warnings: list[str] = []
    last_err: Exception | None = None
    for enc in _ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, header=None, dtype=str, keep_default_na=False)
            # latin-1 是兜底，若中文被读成 mojibake（含大量 Ã 等字符）则视为失败
            if enc == "latin-1":
                sample = "".join(df.iloc[:, 0].astype(str).head(20))
                if any(ch in sample for ch in ("Ã", "å", "æ")):
                    raise UnicodeDecodeError(enc, b"", 0, 1, "疑似乱码")
            return df, enc, warnings
        except (UnicodeDecodeError, UnicodeError) as e:
            last_err = e
            continue
    raise ValueError(f"无法识别 CSV 编码：{last_err}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def import_file(path: str | Path, sheet_header_rows: dict[str, int] | None = None) -> ImportResult:
    """导入 xlsx/xls/csv。

    sheet_header_rows: 手动指定表头行（UI 里置信度低时用户选择），{sheet 名: 行号}。
    """
    path = Path(path)
    check_file_size(path)
    sheet_header_rows = sheet_header_rows or {}
    warnings: list[str] = []
    header_rows: dict[str, int] = {}
    confidence: dict[str, float] = {}
    sheets: dict[str, pd.DataFrame] = {}

    suffix = path.suffix.lower()
    is_csv = suffix == ".csv"
    if suffix in (".xlsx", ".xlsm"):
        raw, merged_warns = _fill_merged_cells(path)
        warnings.extend(merged_warns)
    elif suffix == ".xls":
        raw = {name: df for name, df in pd.read_excel(path, sheet_name=None, header=None).items()}
    elif suffix == ".csv":
        df, enc, _ = read_csv_auto(path)
        raw = {path.stem: df}
        if enc != "utf-8-sig":
            warnings.append(f"CSV 使用 {enc} 编码读取，已自动识别")
    else:
        raise ValueError(f"不支持的文件类型：{suffix}（支持 .xlsx/.xls/.csv）")

    for name, df_raw in raw.items():
        if sheet_header_rows.get(name) is not None:
            chosen, conf = sheet_header_rows[name], 1.0
        else:
            chosen, conf, _cands = detect_header_row(df_raw)
            if conf < 0.62:
                # 置信度低：默认仍取探测行，但明确提示用户可手动指定表头行
                warnings.append(
                    f"工作表「{name}」表头识别置信度较低（{conf:.0%}），当前取第 {chosen + 1} 行，"
                    f"可在左侧『指定表头行』中手动修正"
                )
        df = load_with_header(df_raw, chosen, from_csv=is_csv)
        # 重复列名在 load_with_header 已改名；这里记录提示
        header_rows[name] = chosen
        confidence[name] = conf
        sheets[name] = df

    return ImportResult(sheets=sheets, header_rows=header_rows, warnings=warnings,
                        header_confidence=confidence)
