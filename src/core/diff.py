"""diff 计算：结构化写操作的变更预览（旧值红 / 新值绿，影响单元格计数）。

每个写操作执行前都会先生成 DiffResult 给用户确认（规格 §3.3 / FR-5.5）；
影响单元格超过阈值（50）时 UI 强制二次确认。
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import pandas as pd

# UI 里最多展示的逐格变更条数（超出只显示汇总）
MAX_DIFF_ROWS_SHOWN = 60


@dataclass
class CellChange:
    row_label: str      # 人类可读行标识，如「第 12 行」
    column: str
    old: str
    new: str

    def to_dict(self) -> dict:
        return {"row_label": self.row_label, "column": self.column,
                "old": self.old, "new": self.new}


@dataclass
class DiffResult:
    op_type: str
    sheet: str
    description: str = ""
    n_cells: int = 0                      # 受影响单元格数（确认阈值用它）
    changes: list[CellChange] = field(default_factory=list)   # 截断后的逐格变更
    total_changes: int = 0                # 逐格变更总数
    rows_added: int = 0
    rows_removed: int = 0
    style_changes: dict = field(default_factory=dict)         # {cell_ref: {old, new}}
    extra: dict = field(default_factory=dict)                 # 各操作自定信息（如逆操作摘要）

    def needs_double_confirm(self, threshold: int) -> bool:
        return self.n_cells > threshold

    def to_dict(self) -> dict:
        return {
            "op_type": self.op_type, "sheet": self.sheet,
            "description": self.description, "n_cells": self.n_cells,
            "changes": [c.to_dict() for c in self.changes],
            "total_changes": self.total_changes,
            "rows_added": self.rows_added, "rows_removed": self.rows_removed,
            "style_changes": self.style_changes, "extra": self.extra,
        }

    def _fmt(self, v) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
            return "（空）"
        if isinstance(v, float):
            return f"{v:.4f}".rstrip("0").rstrip(".")
        return str(v)[:60]

    def to_html(self) -> str:
        """渲染为 HTML 表格：旧值红底删除线，新值绿底。"""
        parts = [
            f"<div style='margin:4px 0'><b>操作：</b>{html.escape(self.description or self.op_type)}"
            f"<br/><b>工作表：</b>{html.escape(self.sheet)}　"
            f"<b>影响单元格：</b>{self.n_cells} 个"
            + (f"　<b>行变化：</b>+{self.rows_added} / -{self.rows_removed}" if (self.rows_added or self.rows_removed) else "")
            + "</div>"
        ]
        if self.changes:
            shown = self.changes[:MAX_DIFF_ROWS_SHOWN]
            rows = "".join(
                f"<tr><td style='padding:2px 8px'>{html.escape(c.row_label)}</td>"
                f"<td style='padding:2px 8px'>{html.escape(c.column)}</td>"
                f"<td style='padding:2px 8px;background:#ffe0e0;color:#a00;text-decoration:line-through'>"
                f"{html.escape(self._fmt(c.old))}</td>"
                f"<td style='padding:2px 8px;background:#e0ffe0;color:#060'>"
                f"{html.escape(self._fmt(c.new))}</td></tr>"
                for c in shown
            )
            parts.append(
                "<table style='border-collapse:collapse;font-size:12px'>"
                "<tr><th style='padding:2px 8px'>行</th><th style='padding:2px 8px'>列</th>"
                "<th style='padding:2px 8px'>旧值</th><th style='padding:2px 8px'>新值</th></tr>"
                + rows + "</table>"
            )
            if self.total_changes > len(shown):
                parts.append(f"<div style='font-size:12px;color:#888'>…（其余 {self.total_changes - len(shown)} 处变更省略）</div>")
        if self.style_changes:
            sc = list(self.style_changes.items())[:20]
            rows = "".join(
                f"<tr><td style='padding:2px 8px'>{html.escape(ref)}</td>"
                f"<td style='padding:2px 8px'>{html.escape(str(v.get('old') or '默认样式'))}</td>"
                f"<td style='padding:2px 8px'>{html.escape(str(v.get('new') or '默认样式'))}</td></tr>"
                for ref, v in sc
            )
            parts.append(
                "<table style='border-collapse:collapse;font-size:12px'>"
                "<tr><th style='padding:2px 8px'>单元格</th><th style='padding:2px 8px'>旧样式</th>"
                f"<th style='padding:2px 8px'>新样式</th></tr>{rows}</table>"
            )
        return "".join(parts)
