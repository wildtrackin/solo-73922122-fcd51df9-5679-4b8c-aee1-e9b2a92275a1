"""拒收类校验：宏、外链、大小、结构问题。"""

from __future__ import annotations

import io
import zipfile

CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload(client, content: bytes, name="wb.xlsx", content_type=CT):
    return client.post(
        "/tasks", files={"file": (name, content, content_type)}
    )


def test_reject_macro_vba_project(client, make_workbook):
    content = make_workbook([("S001", 1.0)])
    # 向 xlsx 容器内塞入 vbaProject.bin
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.namelist():
            dst.writestr(item, src.read(item))
        dst.writestr("xl/vbaProject.bin", b"FAKEVBA")
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MACRO_NOT_ALLOWED"


def test_reject_macro_extension(client, make_workbook):
    content = make_workbook([("S001", 1.0)])
    resp = _upload(client, content, name="evil.xlsm")
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "MACRO_NOT_ALLOWED"


def test_reject_external_link_part(client, make_workbook):
    content = make_workbook([("S001", 1.0)])
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.namelist():
            dst.writestr(item, src.read(item))
        dst.writestr(
            "xl/externalLinks/externalLink1.xml",
            b'<externalLink><externalBook r:id="rId1"/></externalLink>',
        )
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EXTERNAL_LINK_NOT_ALLOWED"


def test_reject_external_relation(client, make_workbook):
    content = make_workbook([("S001", 1.0)])
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as src, zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == "xl/_rels/workbook.xml.rels":
                text = data.decode("utf-8").replace(
                    "</Relationships>",
                    '<Relationship Id="rIdExt" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/'
                    '2006/relationships/externalLink" '
                    'Target="other.xlsx" TargetMode="External"/>'
                    "</Relationships>",
                )
                data = text.encode("utf-8")
            dst.writestr(item, data)
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EXTERNAL_LINK_NOT_ALLOWED"


def test_reject_external_formula_text(client, make_workbook):
    content = make_workbook(
        [("S001", 1.0)],
        verdict_formula=lambda r: '=IF([other.xlsx]标定表!B1>1,"PASS","FAIL")',
    )
    resp = _upload(client, content)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EXTERNAL_LINK_NOT_ALLOWED"


def test_reject_oversize(client, make_workbook):
    # 边界：2 MiB + 1 字节的非 zip 垃圾数据也必须在大小关卡被拦
    resp = _upload(client, b"x" * (2 * 1024 * 1024 + 1))
    assert resp.status_code == 413
    assert resp.json()["detail"]["error_code"] == "FILE_TOO_LARGE"


def test_reject_bad_extension(client, make_workbook):
    resp = _upload(client, b"abc", name="notes.txt")
    assert resp.status_code == 400


def test_reject_bad_zip(client):
    resp = _upload(client, b"this is not a zip at all")
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "INVALID_XLSX"


def test_reject_missing_sample_sheet(client):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "其它表"
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "SHEET_NOT_FOUND"


def test_reject_duplicate_sample_id(client, make_workbook):
    content = make_workbook([("DUP", 1.0), ("DUP", 2.0)])
    resp = _upload(client, content)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "DUPLICATE_SAMPLE_ID"


def test_reject_verdict_not_formula(client):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "标定表"
    ws = wb.create_sheet("样本表")
    ws["A1"] = "样本号"
    ws["B1"] = "判定"
    ws["A2"] = "S001"
    ws["B2"] = "PASS"  # 直接写死，不是公式
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VERDICT_NOT_FORMULA"


def test_reject_too_many_rows(client):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "标定表"
    ws = wb.create_sheet("样本表")
    ws["A1"] = "样本号"
    ws["B1"] = "判定"
    for i in range(101):
        ws.cell(row=i + 2, column=1, value=f"S{i:04d}")
        ws.cell(row=i + 2, column=2, value='=IF(1=1,"PASS","FAIL")')
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "TOO_MANY_ROWS"


def test_reject_empty_sample_id(client, make_workbook):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "标定表"
    ws = wb.create_sheet("样本表")
    ws["A1"] = "样本号"
    ws["B1"] = "判定"
    ws["B2"] = '=IF(1=1,"PASS","FAIL")'
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EMPTY_SAMPLE_ID"
