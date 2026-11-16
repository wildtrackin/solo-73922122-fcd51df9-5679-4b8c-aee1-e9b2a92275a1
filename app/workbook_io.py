"""openpyxl 读取逻辑：原缓存值与重算结果。

- 原件只读取、不修改；
- data_only=True 读取 Excel 缓存值（从未由 Excel 保存过时为 None → 未知）；
- 重算后的文件中，错误单元格 data_type == 'e'，整批失败。
"""

from __future__ import annotations

import io

from openpyxl import load_workbook
from openpyxl.cell.cell import Cell

from . import config

# openpyxl 错误类型字符串
ERROR_DATA_TYPE = "e"
# 兜底：以 # 开头的错误文本（#DIV/0!、#N/A、#REF!、#VALUE!、#NAME? 等）
ERROR_TEXT_PREFIX = "#"

VALID_VERDICTS = {"PASS", "FAIL"}


class WorkbookFormulaError(Exception):
    """重算后出现公式错误，按需求整批失败。"""

    def __init__(self, row: int, sample_id: str, error_text: str):
        super().__init__(
            f"样本 {sample_id}（第 {row} 行）公式重算错误：{error_text}"
        )
        self.row = row
        self.sample_id = sample_id
        self.error_text = error_text


class UnexpectedVerdict(Exception):
    """重算结果不是 PASS/FAIL。"""


def normalize_verdict(value: object) -> str:
    """把单元格值归一化为 PASS / FAIL。"""
    if value is None:
        raise UnexpectedVerdict("重算结果为空")
    text = str(value).strip().upper()
    if text not in VALID_VERDICTS:
        raise UnexpectedVerdict(f"重算结果不是 PASS/FAIL：{value!r}")
    return text


def _is_error_cell(cell: Cell) -> str | None:
    if cell.data_type == ERROR_DATA_TYPE:
        return str(cell.value)
    if isinstance(cell.value, str) and cell.value.startswith(ERROR_TEXT_PREFIX):
        return cell.value
    return None


def read_cached_verdicts(content: bytes, rows_info: list[tuple]) -> dict[int, str | None]:
    """从原件读取 B 列缓存判定。

    返回 {行号: "PASS"/"FAIL"/None}。缓存缺失、缓存不是 PASS/FAIL 一律记为未知。
    """
    wb = load_workbook(io.BytesIO(content), data_only=True)
    try:
        ws = wb[config.SAMPLE_SHEET_NAME]
        cached: dict[int, str | None] = {}
        for row_idx, _sid, _cell in rows_info:
            cell = ws.cell(row=row_idx, column=config.VERDICT_COLUMN)
            value = cell.value
            if value is None:
                cached[row_idx] = None
            else:
                text = str(value).strip().upper()
                cached[row_idx] = text if text in VALID_VERDICTS else None
        return cached
    finally:
        wb.close()


def read_recalculated_verdicts(
    recalc_path: str, rows_info: list[tuple]
) -> dict[int, str]:
    """读取 LibreOffice 重算后的 B 列判定，发现公式错误立即抛出。"""
    wb = load_workbook(recalc_path, data_only=True)
    try:
        if config.SAMPLE_SHEET_NAME not in wb.sheetnames:
            raise UnexpectedVerdict("重算后的文件缺少样本表")
        ws = wb[config.SAMPLE_SHEET_NAME]
        results: dict[int, str] = {}
        for row_idx, sample_id, _cell in rows_info:
            cell = ws.cell(row=row_idx, column=config.VERDICT_COLUMN)
            err = _is_error_cell(cell)
            if err is not None:
                raise WorkbookFormulaError(row_idx, sample_id, err)
            results[row_idx] = normalize_verdict(cell.value)
        return results
    finally:
        wb.close()
