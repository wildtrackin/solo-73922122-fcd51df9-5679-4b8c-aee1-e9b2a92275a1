"""核对主流程：缓存缺失、缓存一致、缓存失真（PASS→FAIL / FAIL→PASS）。"""

from __future__ import annotations

import io
import time
import zipfile

import openpyxl


def _upload(client, content: bytes, name="wb.xlsx"):
    return client.post(
        "/tasks",
        files={"file": (name, content,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")},
    )


def _wait_terminal(client, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in (
            "completed", "failed", "cancelled", "interrupted"
        ):
            return data
        time.sleep(0.2)
    raise AssertionError(f"任务 {task_id} 超时未到终态")


def test_missing_cache_marked_unknown(client, make_workbook):
    # openpyxl 直接保存的文件没有任何公式缓存 → 全部未知
    content = make_workbook([("S001", 20.0), ("S002", 5.0)])
    resp = _upload(client, content)
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    result = _wait_terminal(client, task_id)
    assert result["status"] == "completed"
    samples = result["samples"]
    assert len(samples) == 2
    by_id = {s["sample_id"]: s for s in samples}
    assert by_id["S001"]["cached_verdict"] is None
    assert by_id["S001"]["change_type"] == "cache_unknown"
    assert by_id["S001"]["recalculated_verdict"] == "PASS"
    assert by_id["S002"]["recalculated_verdict"] == "FAIL"
    assert result["summary"]["cache_unknown"] == 2


def test_cache_drift_pass_to_fail(client, make_workbook, recalc_xlsx):
    raw = make_workbook([("S001", 20.0), ("S002", 5.0), ("S003", 9.0)])
    good = recalc_xlsx(raw)  # 真实缓存：PASS FAIL FAIL

    # 把 S003 的缓存 FAIL 伪造成 PASS（缓存失真）
    with zipfile.ZipFile(io.BytesIO(good)) as zf:
        names = {n: zf.read(n) for n in zf.namelist()}
    # 找到带公式的样本表 sheet
    candidates = [
        n for n in names
        if n.startswith("xl/worksheets/sheet")
        and (b"<f " in names[n] or b"<f>" in names[n])
    ]
    assert candidates, "未找到含公式的 sheet 部件"
    # 样本表是第二个 sheet，取名称排序最后一个含公式的
    sheet_name = sorted(candidates)[-1]
    xml = names[sheet_name].decode("utf-8")
    assert xml.count("<v>FAIL</v>") >= 1
    # 仅替换最后一个 FAIL（S003 所在行靠后）
    pos = xml.rfind("<v>FAIL</v>")
    xml = xml[:pos] + "<v>PASS</v>" + xml[pos + len("<v>FAIL</v>"):]
    names[sheet_name] = xml.encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, data in names.items():
            zf.writestr(n, data)

    result = _wait_terminal(client, _upload(client, buf.getvalue()).json()["task_id"])
    assert result["status"] == "completed"
    by_id = {s["sample_id"]: s for s in result["samples"]}
    assert by_id["S001"]["change_type"] == "unchanged"
    assert by_id["S002"]["change_type"] == "unchanged"
    drift = by_id["S003"]
    assert drift["cached_verdict"] == "PASS"
    assert drift["recalculated_verdict"] == "FAIL"
    assert drift["change_type"] == "pass_to_fail"
    assert drift["changed"] is True
    assert result["summary"]["pass_to_fail"] == 1


def test_sample_file_detects_drift(client):
    with open("samples/cache_drift_sample.xlsx", "rb") as f:
        content = f.read()
    resp = _upload(client, content, "cache_drift_sample.xlsx")
    assert resp.status_code == 202, resp.text
    result = _wait_terminal(client, resp.json()["task_id"])
    assert result["status"] == "completed"
    by_id = {s["sample_id"]: s for s in result["samples"]}
    assert by_id["S004"]["cached_verdict"] == "PASS"
    assert by_id["S004"]["recalculated_verdict"] == "FAIL"
    assert by_id["S005"]["change_type"] == "pass_to_fail"
    assert result["summary"]["drift_pass_to_fail"] == 2
    # 原件未被修改：服务端暂存已清，文件哈希不变由其它测试保证


def test_cached_consistent(client, make_workbook, recalc_xlsx):
    raw = make_workbook([("S001", 20.0), ("S002", 5.0)])
    good = recalc_xlsx(raw)
    result = _wait_terminal(client, _upload(client, good).json()["task_id"])
    assert result["status"] == "completed"
    assert all(not s["changed"] for s in result["samples"])
    assert result["summary"]["changed"] == 0
    assert result["summary"]["unchanged"] == 2


def test_cache_drift_fail_to_pass(client, make_workbook, recalc_xlsx):
    # 真实结果 FAIL（测量值 5 < 阈值 12），把缓存伪造成 FAIL→PASS 的反向：
    # 这里构造“缓存为 FAIL、重算为 PASS”——用高测量值但篡改缓存
    raw = make_workbook([("S001", 99.0)])  # 重算必然 PASS
    good = recalc_xlsx(raw)
    # 把唯一的 PASS 缓存改成 FAIL，制造 fail_to_pass
    with zipfile.ZipFile(io.BytesIO(good)) as zf:
        names = {n: zf.read(n) for n in zf.namelist()}
    candidates = [
        n for n in names
        if n.startswith("xl/worksheets/sheet")
        and (b"<f " in names[n] or b"<f>" in names[n])
    ]
    sheet_name = sorted(candidates)[-1]
    xml = names[sheet_name].decode("utf-8")
    assert "<v>PASS</v>" in xml
    xml = xml.replace("<v>PASS</v>", "<v>FAIL</v>", 1)
    names[sheet_name] = xml.encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, data in names.items():
            zf.writestr(n, data)

    result = _wait_terminal(client, _upload(client, buf.getvalue()).json()["task_id"])
    assert result["status"] == "completed"
    s = result["samples"][0]
    assert s["cached_verdict"] == "FAIL"
    assert s["recalculated_verdict"] == "PASS"
    assert s["change_type"] == "fail_to_pass"
    assert result["summary"]["fail_to_pass"] == 1


def test_unexpected_verdict_fails_batch(client):
    # 公式返回的不是 PASS/FAIL（这里返回数字），整批失败
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "标定表"
    ws = wb.create_sheet("样本表")
    ws["A1"] = "样本号"
    ws["B1"] = "判定"
    ws["A2"] = "S001"
    ws["B2"] = "=1+1"
    buf = io.BytesIO()
    wb.save(buf)
    result = _wait_terminal(
        client, _upload(client, buf.getvalue()).json()["task_id"]
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "UNEXPECTED_VERDICT"
    assert result["samples"] is None
