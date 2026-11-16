"""上传工作簿的拒收类安全/结构校验。

拒收（HTTP 4xx，不创建任务）的情形：
- 非 xlsx（zip OOXML）容器
- 含 VBA 宏（vbaProject.bin、.xlsm 部件、Content_Types 中的宏类型）
- 含外部链接部件（externalLinks、外部连接、外部引用）
- 公式文本引用外部工作簿（[xxx.xlsx] 或盘符/网络路径）
- 缺少「样本表」或列结构不符合要求
- 样本号缺失/重复、数据行数超限、B 列不是公式
"""

from __future__ import annotations

import io
import re
import zipfile

from openpyxl import load_workbook

from . import config

# 外部工作簿引用特征：[工作簿名]Sheet!A1、盘符路径、UNC 路径
EXTERNAL_WORKBOOK_PATTERN = re.compile(
    r"""(
        \[[^\[\]]+\.(xlsx|xlsm|xls|xlsb|csv|ods)\]  # [book.xlsx] 形式
        |(?:^|[^a-z_])[a-zA-Z]:\\                     # C:\ 盘符
        |\\\\                                          # \\ UNC 网络路径
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# 外链/外部连接在 zip 部件名或 content-type 里的特征
EXTERNAL_PART_PATTERNS = (
    "externalLinks/",
    "externalconnections/",
    "externalLink",
    "connections.xml",
    "xl/queryTables/",
    "xl/customXml/",
)
MACRO_PART_PATTERNS = (
    "vbaProject.bin",
    "vbaProject",
    "/xl/vba",
)
MACRO_CONTENT_TYPES = (
    "vnd.ms-excel.vbaProject",
    "sheet.macroEnabled",
    "template.macroEnabled",
    "addin.macroEnabled",
)


class WorkbookRejected(Exception):
    """工作簿被拒收（调用方映射为 HTTP 4xx）。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def scan_container(content: bytes, filename: str) -> None:
    """在 zip 容器层面扫描宏与外链。原件字节不做任何修改。"""
    if filename.lower().endswith((".xlsm", ".xltm", ".xlam", ".xls")):
        raise WorkbookRejected(
            "MACRO_NOT_ALLOWED",
            "拒收宏工作簿或旧版二进制格式，仅允许 .xlsx",
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise WorkbookRejected(
            "INVALID_XLSX", "文件不是有效的 xlsx（zip OOXML）容器"
        ) from exc

    bad_entry = zf.testzip()
    if bad_entry is not None:
        raise WorkbookRejected("INVALID_XLSX", f"xlsx 压缩包损坏：{bad_entry}")

    names = zf.namelist()
    for name in names:
        low = name.lower()
        if any(p.lower() in low for p in MACRO_PART_PATTERNS):
            raise WorkbookRejected(
                "MACRO_NOT_ALLOWED", f"工作簿含 VBA 宏部件，拒收：{name}"
            )
        if any(p.lower() in low for p in EXTERNAL_PART_PATTERNS):
            raise WorkbookRejected(
                "EXTERNAL_LINK_NOT_ALLOWED",
                f"工作簿含外部链接/外部连接部件，拒收：{name}",
            )

    # [Content_Types].xml 中的宏与外部链接声明
    try:
        content_types = zf.read("[Content_Types].xml").decode("utf-8", "ignore")
    except KeyError:
        raise WorkbookRejected("INVALID_XLSX", "缺少 [Content_Types].xml")
    low_ct = content_types.lower()
    if any(t in low_ct for t in MACRO_CONTENT_TYPES):
        raise WorkbookRejected(
            "MACRO_NOT_ALLOWED", "Content_Types 声明了宏内容类型，拒收"
        )
    if "externallink" in low_ct or "externalconnection" in low_ct:
        raise WorkbookRejected(
            "EXTERNAL_LINK_NOT_ALLOWED",
            "Content_Types 声明了外部链接内容类型，拒收",
        )

    # 关系文件里的外部关系（TargetMode="External"）
    for name in names:
        if name.endswith(".rels"):
            text = zf.read(name).decode("utf-8", "ignore")
            if 'TargetMode="External"'.lower() in text.lower():
                raise WorkbookRejected(
                    "EXTERNAL_LINK_NOT_ALLOWED",
                    f"关系部件含外部目标，拒收：{name}",
                )


def _scan_formula_text(formula: str | None) -> None:
    if not formula:
        return
    if EXTERNAL_WORKBOOK_PATTERN.search(formula):
        raise WorkbookRejected(
            "EXTERNAL_LINK_NOT_ALLOWED",
            f"公式引用了外部工作簿/路径，拒收：{formula[:80]}",
        )


def inspect_workbook(content: bytes) -> dict:
    """用 openpyxl 做结构校验并抽取样本行信息。

    返回 {"sheet": 工作表, "rows": [(row_idx, sample_id, formula_cell), ...]}。
    原件仅被读取，不落盘修改。
    """
    try:
        wb_formula = load_workbook(io.BytesIO(content), data_only=False)
    except Exception as exc:  # openpyxl 可能抛多种异常
        raise WorkbookRejected(
            "INVALID_XLSX", f"openpyxl 无法解析工作簿：{exc}"
        ) from exc

    if config.SAMPLE_SHEET_NAME not in wb_formula.sheetnames:
        raise WorkbookRejected(
            "SHEET_NOT_FOUND",
            f"缺少名为「{config.SAMPLE_SHEET_NAME}」的样本表",
        )
    ws = wb_formula[config.SAMPLE_SHEET_NAME]

    rows: list[tuple[int, str, object]] = []
    seen_ids: set[str] = set()
    max_row = ws.max_row or 0
    for row_idx in range(config.HEADER_ROWS + 1, max_row + 1):
        id_cell = ws.cell(row=row_idx, column=config.SAMPLE_ID_COLUMN)
        verdict_cell = ws.cell(row=row_idx, column=config.VERDICT_COLUMN)
        sample_id = id_cell.value
        # 完全空行跳过；A 列空但 B 列有内容属于结构错误
        if sample_id is None and (verdict_cell.value is None):
            continue
        if sample_id is None or str(sample_id).strip() == "":
            raise WorkbookRejected(
                "EMPTY_SAMPLE_ID", f"第 {row_idx} 行 A 列样本号为空"
            )
        sid = str(sample_id).strip()
        if sid in seen_ids:
            raise WorkbookRejected(
                "DUPLICATE_SAMPLE_ID", f"样本号重复：{sid}（第 {row_idx} 行）"
            )
        seen_ids.add(sid)

        formula = verdict_cell.value
        if not (isinstance(formula, str) and formula.startswith("=")):
            raise WorkbookRejected(
                "VERDICT_NOT_FORMULA",
                f"第 {row_idx} 行 B 列判定不是公式：{formula!r}",
            )
        _scan_formula_text(formula)
        rows.append((row_idx, sid, verdict_cell))

        if len(rows) > config.MAX_SAMPLE_ROWS:
            raise WorkbookRejected(
                "TOO_MANY_ROWS",
                f"样本数据行数超过上限 {config.MAX_SAMPLE_ROWS} 行",
            )

    if not rows:
        raise WorkbookRejected("NO_SAMPLE_ROWS", "样本表没有任何样本数据行")

    # 顺带扫描其它工作表的公式外链（标定表等）
    for other in wb_formula.worksheets:
        if other is ws:
            continue
        for row in other.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    _scan_formula_text(cell.value)

    wb_formula.close()
    return {"rows": rows}
