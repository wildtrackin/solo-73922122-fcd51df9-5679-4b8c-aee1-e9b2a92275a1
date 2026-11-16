"""pytest 共享夹具：隔离的 DATA_DIR、便携/系统 LibreOffice、HTTP 测试客户端。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _soffice_available() -> bool:
    bin_path = os.environ.get("SOFFICE_BIN", "soffice")
    if "/" in bin_path:
        return Path(bin_path).exists()
    # 容器/CI 里 soffice 在 PATH 上
    for d in os.environ.get("PATH", "").split(":"):
        if (Path(d) / bin_path).exists():
            return True
    return False


@pytest.fixture(scope="session")
def tmp_data_dir():
    with tempfile.TemporaryDirectory(prefix="verify-data-") as d:
        yield Path(d)


@pytest.fixture(scope="session")
def client(tmp_data_dir):
    if not _soffice_available():
        pytest.skip("未找到 LibreOffice（设置 SOFFICE_BIN/SOFFICE_LD_LIBRARY_PATH）")

    os.environ["DATA_DIR"] = str(tmp_data_dir)
    os.environ.setdefault("RECALC_TIMEOUT_SECONDS", "10")
    os.environ.setdefault("MAX_CONCURRENT_BATCHES", "2")

    # 延迟导入，确保环境变量先生效
    from app import config
    config.DATA_DIR = tmp_data_dir
    config.STAGING_DIR = tmp_data_dir / "staging"
    config.TASKS_DIR = tmp_data_dir / "tasks"
    config.SOFFICE_BIN = os.environ.get("SOFFICE_BIN", "soffice")

    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_workbook():
    """生成工作簿字节的工厂。"""
    import io
    import openpyxl

    def _make(samples, threshold=12, sheet_name="样本表", verdict_formula=None):
        wb = openpyxl.Workbook()
        cal = wb.active
        cal.title = "标定表"
        cal["A2"] = "合格阈值"
        cal["B2"] = threshold
        ws = wb.create_sheet(sheet_name)
        ws["A1"] = "样本号"
        ws["B1"] = "判定"
        ws["C1"] = "测量值"
        for idx, (sid, value) in enumerate(samples, start=2):
            ws.cell(row=idx, column=1, value=sid)
            if verdict_formula is None:
                formula = f'=IF(C{idx}>=标定表!$B$2,"PASS","FAIL")'
            else:
                formula = verdict_formula(idx)
            ws.cell(row=idx, column=2, value=formula)
            ws.cell(row=idx, column=3, value=value)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    return _make


@pytest.fixture()
def recalc_xlsx():
    """用 LibreOffice 重算并写入真实缓存，返回新文件字节。"""
    import shutil
    import subprocess

    def _do(content: bytes) -> bytes:
        work = Path(tempfile.mkdtemp(prefix="recalc-"))
        try:
            src = work / "in.xlsx"
            src.write_bytes(content)
            out_dir = work / "out"
            out_dir.mkdir()
            profile = work / "prof"
            (profile / "user").mkdir(parents=True)
            (profile / "user" / "registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Calc/Formula/Load">'
                '<prop oor:name="OOXMLRecalcMode" oor:op="fuse">'
                "<value>0</value></prop></item></oor:items>",
                encoding="utf-8",
            )
            env = dict(os.environ)
            # 便携版 LibreOffice 需要显式动态库路径（容器 apt 版该变量为空）
            extra = os.environ.get("SOFFICE_LD_LIBRARY_PATH")
            if extra:
                env["LD_LIBRARY_PATH"] = extra
            cmd = [
                os.environ.get("SOFFICE_BIN", "soffice"),
                "--headless", "--norestore", "--nolockcheck",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to", "xlsx", "--outdir", str(out_dir), str(src),
            ]
            subprocess.run(cmd, check=True, env=env, capture_output=True, timeout=60)
            return (out_dir / "in.xlsx").read_bytes()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return _do
