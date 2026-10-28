"""schema 抽取：DataFrame → 列级 schema（类型/缺失率/样本值/唯一性/异常标记）。

铁律：表数据永不整块进 LLM 上下文，进入上下文的只有这里产出的 schema 摘要
（列名、类型、样本值×3、行数、缺失率、异常标记）与统计摘要。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

# 常见中文日期格式（疑似日期文本探测用）
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%d/%m/%Y", "%m/%d/%Y")


@dataclass
class ColumnSchema:
    name: str
    dtype: str                  # int / float / string / datetime / bool / empty
    nullable: bool
    missing_rate: float         # 0~1
    samples: list = field(default_factory=list)   # 3 个非空样本值（字符串化）
    is_unique: bool = False
    n_unique: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SheetSchema:
    sheet_name: str
    n_rows: int
    n_cols: int
    columns: list[ColumnSchema]

    def to_dict(self) -> dict:
        return {
            "sheet_name": self.sheet_name,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "columns": [c.to_dict() for c in self.columns],
        }

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_prompt(self, max_columns: int = 60) -> str:
        """渲染成喂给 LLM 的紧凑文本（绝不包含整列数据，只含样本值×3）。"""
        lines = [
            f"工作表「{self.sheet_name}」：{self.n_rows} 行 × {self.n_cols} 列",
        ]
        for c in self.columns[:max_columns]:
            line = f"- {c.name}（{c.dtype}，缺失率 {c.missing_rate:.0%}）样本: {c.samples}"
            if c.warnings:
                line += f" [提示] {'、'.join(c.warnings)}"
            lines.append(line)
        if len(self.columns) > max_columns:
            lines.append(f"...（另有 {len(self.columns) - max_columns} 列未列出）")
        return "\n".join(lines)


def _sniff_object_column(series: pd.Series) -> tuple[str, list[str]]:
    """对 object 列做嗅探：判断是否文本型数值 / 疑似日期文本。"""
    non_null = series.dropna()
    warnings: list[str] = []
    if non_null.empty:
        return "empty", ["全空列"]

    sample = non_null.astype(str).head(200)
    # 文本型数值：去掉千分位/货币符号后大部分可转数字
    def _to_num(v: str):
        v = str(v).replace(",", "").replace("￥", "").replace("¥", "").replace(" ", "")
        try:
            return float(v)
        except ValueError:
            return None

    num_ok = sum(1 for v in sample if _to_num(v) is not None)
    if num_ok / len(sample) >= 0.8:
        warnings.append("文本型数值")
        return "string", warnings

    # 疑似日期文本
    def _is_date(v: str) -> bool:
        v = str(v).strip()
        for fmt in _DATE_FORMATS:
            try:
                pd.to_datetime(v, format=fmt)
                return True
            except (ValueError, TypeError, pd._libs.tslibs.parsing.DateParseError):
                continue
        return False

    date_ok = sum(1 for v in sample if _is_date(v))
    if date_ok / len(sample) >= 0.8:
        warnings.append("疑似日期文本")
        return "string", warnings

    return "string", warnings


def extract_schema(df: pd.DataFrame, sheet_name: str = "Sheet1") -> SheetSchema:
    """从 DataFrame 抽取列级 schema 并标记异常（规格 FR-2.4）。"""
    columns: list[ColumnSchema] = []
    n_rows = len(df)

    # 重复列名检查（导入器一般已改名，这里兜底标记）
    dup_names = set(df.columns[df.columns.duplicated()])

    for col in df.columns:
        series = df[col]
        missing = int(series.isna().sum())
        missing_rate = missing / n_rows if n_rows else 0.0
        non_null = series.dropna()

        warnings: list[str] = []
        if col in dup_names:
            warnings.append("重复列名")

        if series.empty or non_null.empty:
            columns.append(ColumnSchema(
                name=str(col), dtype="empty", nullable=True, missing_rate=1.0,
                samples=[], warnings=warnings + ["全空列"],
            ))
            continue

        # 用 pandas 类型判断而非字符串前缀：pandas 可空类型是 "Float64"/"Int64"/"boolean"（大写）
        if pd.api.types.is_bool_dtype(series):
            inferred = "bool"
        elif pd.api.types.is_integer_dtype(series):
            inferred = "int"
        elif pd.api.types.is_float_dtype(series):
            inferred = "float"
        elif pd.api.types.is_datetime64_any_dtype(series):
            inferred = "datetime"
        else:
            inferred, sniff_warn = _sniff_object_column(series)
            warnings.extend(sniff_warn)

        samples = [str(v)[:40] for v in non_null.head(50).unique()[:3]]
        n_unique = int(non_null.nunique())
        columns.append(ColumnSchema(
            name=str(col),
            dtype=inferred,
            nullable=bool(missing > 0),
            missing_rate=round(missing_rate, 4),
            samples=samples,
            is_unique=bool(n_unique == len(non_null) and len(non_null) > 0),
            n_unique=n_unique,
            warnings=warnings,
        ))

    return SheetSchema(
        sheet_name=sheet_name, n_rows=n_rows, n_cols=len(df.columns), columns=columns,
    )


def numeric_summary(df: pd.DataFrame, max_columns: int = 12) -> str:
    """数值列统计摘要（极值/均值），供数据卡片与分析上下文（FR-2.2）。"""
    parts: list[str] = []
    for col in df.select_dtypes(include=[np.number]).columns[:max_columns]:
        s = df[col].dropna()
        if s.empty:
            continue
        parts.append(
            f"{col}: 均值 {s.mean():.4g}，最小 {s.min():.4g}，最大 {s.max():.4g}，非空 {len(s)}"
        )
    return "；".join(parts) if parts else "无数值列"
