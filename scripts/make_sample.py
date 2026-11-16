"""生成「缓存失真」样例工作簿。

背景：Excel/xlsx 的公式结果以缓存值存在文件里。若标定表参数被改动、
但工作簿没有在 Excel/WPS 里重新打开保存，缓存判定就会停留在旧值。

流程：
1. openpyxl 建簿：标定表给出阈值 12，样本表 B 列用 IF 公式判定 PASS/FAIL；
2. LibreOffice headless 强制重算，得到真实缓存（S004/S005 实为 FAIL）；
3. 直接改写 sheet XML，把 S004、S005 的缓存值从 FAIL 伪造成 PASS，
   模拟“参数已变、缓存未刷新”的失真文件（公式本身不动）。

用法：
  python scripts/make_sample.py [输出目录]
环境变量 SOFFICE_BIN / SOFFICE_LD_LIBRARY_PATH 可指定便携版 LibreOffice。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import openpyxl

THRESHOLD = 12  # 标定表阈值：测量值 < 12 判 FAIL
# (样本号, 测量值, 真实重算判定, 伪造缓存)
SAMPLES = [
    ("S001", 25.6, "PASS", None),
    ("S002", 18.0, "PASS", None),
    ("S003", 12.0, "PASS", None),   # 边界：等于阈值判 PASS
    ("S004", 9.8, "FAIL", "PASS"),  # 缓存失真：真 FAIL，缓存伪 PASS
    ("S005", 3.2, "FAIL", "PASS"),  # 缓存失真：真 FAIL，缓存伪 PASS
]


def build_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    cal = wb.active
    cal.title = "标定表"
    cal["A1"] = "参数"
    cal["B1"] = "数值"
    cal["A2"] = "合格阈值"
    cal["B2"] = THRESHOLD

    samp = wb.create_sheet("样本表")
    samp["A1"] = "样本号"
    samp["B1"] = "判定"
    samp["C1"] = "测量值"
    for idx, (sid, value, _real, _fake) in enumerate(SAMPLES, start=2):
        samp.cell(row=idx, column=1, value=sid)
        samp.cell(
            row=idx, column=2,
            value=f'=IF(C{idx}>=标定表!$B$2,"PASS","FAIL")',
        )
        samp.cell(row=idx, column=3, value=value)
    wb.save(path)


def recalc_with_libreoffice(src: Path, out_dir: Path) -> Path:
    soffice = os.environ.get("SOFFICE_BIN", "soffice")
    env = dict(os.environ)
    extra = os.environ.get("SOFFICE_LD_LIBRARY_PATH")
    if extra:
        env["LD_LIBRARY_PATH"] = extra
    profile = out_dir / "profile"
    (profile / "user").mkdir(parents=True, exist_ok=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
  <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
 </item>
</oor:items>""",
        encoding="utf-8",
    )
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck",
        "--nodefault", "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile.resolve()}",
        "--convert-to", "xlsx", "--outdir", str(out_dir.resolve()),
        str(src.resolve()),
    ]
    subprocess.run(cmd, check=True, env=env, capture_output=True, timeout=60)
    out = out_dir / src.name
    if not out.exists():
        raise RuntimeError("LibreOffice 未产出重算文件")
    return out


def corrupt_cached_verdicts(recalc_file: Path, target: Path) -> None:
    """把 S004/S005（第 5、6 行）B 列缓存 FAIL 改写成 PASS。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(recalc_file) as zf:
            zf.extractall(tmp_path)

        # 找到「样本表」对应的 sheet XML
        workbook_rels = (
            tmp_path / "xl" / "_rels" / "workbook.xml.rels"
        ).read_text(encoding="utf-8")
        wb_xml = (tmp_path / "xl" / "workbook.xml").read_text(encoding="utf-8")
        import re
        sheet_rid = re.search(
            r'<sheet[^>]*name="样本表"[^>]*r:id="(rId\d+)"', wb_xml
        ).group(1)
        sheet_target = re.search(
            rf'Id="{sheet_rid}"[^>]*Target="([^"]+)"', workbook_rels
        ).group(1)
        sheet_path = (tmp_path / "xl" / sheet_target).resolve()

        xml = sheet_path.read_text(encoding="utf-8")
        # 这两个 FAIL 串只出现在 S004/S005 的缓存 <v>FAIL</v>
        occurrences = xml.count("<v>FAIL</v>")
        if occurrences != 2:
            raise RuntimeError(
                f"预期 2 处 FAIL 缓存，实际 {occurrences} 处，样例生成中止"
            )
        xml = xml.replace("<v>FAIL</v>", "<v>PASS</v>")
        sheet_path.write_text(xml, encoding="utf-8")

        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(tmp_path):
                for name in files:
                    full = Path(root) / name
                    rel = full.relative_to(tmp_path)
                    zf.write(full, rel.as_posix())


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "cache_drift_sample.xlsx"

    with tempfile.TemporaryDirectory() as work:
        work_path = Path(work)
        src = work_path / "cache_drift_sample.xlsx"
        build_workbook(src)
        recalc_dir = work_path / "recalc"
        recalc_dir.mkdir()
        recalc_file = recalc_with_libreoffice(src, recalc_dir)
        corrupt_cached_verdicts(recalc_file, target)

    # 自检：确认缓存显示 PASS 而公式重算应为 FAIL
    wb_cached = openpyxl.load_workbook(target, data_only=True)
    ws = wb_cached["样本表"]
    print("失真缓存（openpyxl data_only 读到的旧值）：")
    for row in range(2, 7):
        print(f"  {ws.cell(row=row, column=1).value}: "
              f"缓存={ws.cell(row=row, column=2).value}")
    print(f"\n样例已生成：{target}")
    print("预期：S004/S005 缓存为 PASS，提交核对 API 重算后变为 FAIL。")


if __name__ == "__main__":
    main()
