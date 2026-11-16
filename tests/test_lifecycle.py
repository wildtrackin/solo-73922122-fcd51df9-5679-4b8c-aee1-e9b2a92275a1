"""任务生命周期：公式错误、取消、并发隔离、重启恢复、终态保护。"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload(client, content: bytes, name="wb.xlsx"):
    return client.post(
        "/tasks", files={"file": (name, content, CT)}
    )


def _wait_terminal(client, task_id: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/tasks/{task_id}").json()
        if data["status"] in (
            "completed", "failed", "cancelled", "interrupted"
        ):
            return data
        time.sleep(0.1)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到终态")


def test_formula_error_fails_whole_batch(client, make_workbook):
    # 一个样本除零错误：整批失败且不发布结果
    content = make_workbook(
        [("OK1", 20.0), ("BAD", 5.0)],
        verdict_formula=lambda r: '=IF(1/0,"PASS","FAIL")'
        if r == 3 else f'=IF(C{r}>=标定表!$B$2,"PASS","FAIL")',
    )
    resp = _upload(client, content)
    assert resp.status_code == 202
    result = _wait_terminal(client, resp.json()["task_id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "FORMULA_ERROR"
    assert result["samples"] is None
    assert "BAD" in (result["error_message"] or "")


def test_cancel_terminal_not_allowed(client, make_workbook):
    content = make_workbook([("S001", 1.0)])
    task_id = _upload(client, content).json()["task_id"]
    _wait_terminal(client, task_id)
    resp = client.post(f"/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancel_requested"] is True
    assert "不可取消" in body["message"]
    assert body["status"] == "completed"


def test_cancel_unknown_task_404(client):
    resp = client.post("/tasks/nonexistent/cancel")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "TASK_NOT_FOUND"


@pytest.mark.asyncio
async def test_queued_cancel_is_immediate(monkeypatch, tmp_path):
    """纯 asyncio：占满名额后新任务排队，取消排队任务立即落 cancelled。"""
    import io
    import openpyxl
    from app import config
    data_dir = tmp_path / "queued-data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "TASKS_DIR", data_dir / "tasks")
    monkeypatch.setattr(config, "STAGING_DIR", data_dir / "staging")
    monkeypatch.setattr(config, "SOFFICE_BIN", os.environ.get("SOFFICE_BIN", "soffice"))

    from app.manager import TaskManager
    mgr = TaskManager()
    # 容量降为 1 并占住唯一名额
    mgr.slots._capacity = 1
    await mgr.slots.acquire()
    try:
        wb = openpyxl.Workbook()
        wb.active.title = "标定表"
        ws = wb.create_sheet("样本表")
        ws["A1"] = "样本号"
        ws["B1"] = "判定"
        ws["A2"] = "Q1"
        ws["B2"] = '=IF(1=1,"PASS","FAIL")'
        buf = io.BytesIO()
        wb.save(buf)

        rec = await mgr.create_task(buf.getvalue(), "q.xlsx")
        await asyncio.sleep(0.1)
        assert (await mgr.get(rec["task_id"]))["status"] == "queued"

        got, accepted = await mgr.request_cancel(rec["task_id"])
        assert accepted is True
        final = await mgr.get(rec["task_id"])
        assert final["status"] == "cancelled"
        assert final["samples"] is None
        # 排队任务从未运行，没有暂存残留
        assert not (config.STAGING_DIR / rec["task_id"]).exists()
        # 再次取消被拒绝（终态保护）
        _rec, again = await mgr.request_cancel(rec["task_id"])
        assert again is False
    finally:
        await mgr.slots.release()
        mgr.slots._capacity = 2


def test_concurrent_two_batches_and_slot_release(client, make_workbook):
    # 连续提交 4 批，全部应完成；任意时刻在跑任务不超过 2
    ids = []
    for i in range(4):
        content = make_workbook([(f"S{i}-{j}", float(j)) for j in range(3)])
        ids.append(_upload(client, content).json()["task_id"])

    running_seen = 0
    deadline = time.time() + 60
    pending = set(ids)
    while pending and time.time() < deadline:
        statuses = [client.get(f"/tasks/{t}").json()["status"] for t in ids]
        running = sum(s == "running" for s in statuses)
        running_seen = max(running_seen, running)
        assert running <= 2, f"并发超出 2：{statuses}"
        pending = {
            t for t in pending
            if client.get(f"/tasks/{t}").json()["status"] not in (
                "completed", "failed", "cancelled", "interrupted"
            )
        }
        time.sleep(0.1)

    assert not pending
    assert running_seen >= 1
    for t in ids:
        final = _wait_terminal(client, t)
        assert final["status"] == "completed"

    # 名额已全部释放，健康检查归零
    health = client.get("/health").json()
    assert health["slots_in_use"] == 0
    assert health["slots_capacity"] == 2


def test_staging_cleaned_after_completion(client, make_workbook, tmp_data_dir):
    content = make_workbook([("S001", 1.0), ("S002", 2.0)])
    task_id = _upload(client, content).json()["task_id"]
    _wait_terminal(client, task_id)
    task_dir = tmp_data_dir / "staging" / task_id
    assert not task_dir.exists(), "终态后暂存目录必须清空"
    # 任务记录仍持久化可查
    record_file = tmp_data_dir / "tasks" / f"{task_id}.json"
    assert record_file.exists()
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert len(record["samples"]) == 2


def test_restart_marks_interrupted(tmp_path, monkeypatch):
    """模拟重启：构造一个 QUEUED 持久化记录，新管理器恢复时应为 interrupted。"""
    data_dir = tmp_path / "data"
    (data_dir / "tasks").mkdir(parents=True)
    (data_dir / "staging").mkdir(parents=True)
    stale_id = "stale1234"
    (data_dir / "staging" / stale_id).mkdir()
    (data_dir / "staging" / stale_id / "input.xlsx").write_bytes(b"x")
    record = {
        "task_id": stale_id,
        "status": "running",
        "filename": "w.xlsx",
        "size_bytes": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:01+00:00",
        "finished_at": None,
        "error_code": None,
        "error_message": None,
        "cancel_requested": False,
        "samples": None,
        "summary": None,
    }
    (data_dir / "tasks" / f"{stale_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    from app import config
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "TASKS_DIR", data_dir / "tasks")
    monkeypatch.setattr(config, "STAGING_DIR", data_dir / "staging")

    from app.manager import TaskManager
    mgr = TaskManager()
    mgr.recover_on_startup()

    recovered = mgr._tasks[stale_id]
    assert recovered["status"] == "interrupted"
    assert recovered["error_code"] == "INTERRUPTED_BY_RESTART"
    # 暂存被清理
    assert not (data_dir / "staging" / stale_id).exists()

    # 已完成的任务重启后状态保持不变
    done_id = "done5678"
    done = dict(record, task_id=done_id, status="completed",
                samples=[{"row": 2, "sample_id": "X", "cached_verdict": "PASS",
                          "recalculated_verdict": "PASS", "changed": False,
                          "change_type": "unchanged"}],
                summary={"total": 1})
    (data_dir / "tasks" / f"{done_id}.json").write_text(
        json.dumps(done), encoding="utf-8"
    )
    mgr.recover_on_startup()
    assert mgr._tasks[done_id]["status"] == "completed"


@pytest.mark.asyncio
async def test_recalc_timeout_releases_slot(monkeypatch, tmp_path):
    """真实 LibreOffice + 1 秒阈值：重算超时落 FAILED，不发布结果并释放名额。"""
    import os as _os
    heavy = "/tmp/heavy.xlsx"
    if not _os.path.exists(heavy):
        pytest.skip("缺少重型工作簿 /tmp/heavy.xlsx")
    from app import config
    data_dir = tmp_path / "timeout-data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "TASKS_DIR", data_dir / "tasks")
    monkeypatch.setattr(config, "STAGING_DIR", data_dir / "staging")
    monkeypatch.setattr(config, "RECALC_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(config, "SOFFICE_BIN", _os.environ.get("SOFFICE_BIN", "soffice"))

    from app.manager import TaskManager
    mgr = TaskManager()
    record = await mgr.create_task(open(heavy, "rb").read(), "heavy.xlsx")
    await mgr._jobs[record["task_id"]]
    final = await mgr.get(record["task_id"])
    assert final["status"] == "failed"
    assert final["error_code"] == "RECALC_TIMEOUT"
    assert final["samples"] is None
    assert mgr.slots.in_use == 0
    assert not (config.STAGING_DIR / record["task_id"]).exists()

@pytest.mark.asyncio
async def test_running_cancel_kills_recalc(monkeypatch, tmp_path):
    """用假的重算协程验证：运行中取消会走 CANCELLED 终态并释放名额。"""
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "staging")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "staging").mkdir()

    from app import manager as manager_mod
    from app.manager import TaskManager
    from app.models import TaskStatus

    mgr = TaskManager()
    cancelled_inside = asyncio.Event()

    # 构造一个真实可读的工作簿作为原件（缓存读取在重算之前）
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = "标定表"
    ws = wb.create_sheet("样本表")
    ws["A1"] = "样本号"
    ws["B1"] = "判定"
    ws["A2"] = "S001"
    ws["B2"] = '=IF(1=1,"PASS","FAIL")'
    buf = io.BytesIO()
    wb.save(buf)

    async def fake_recalc(task_dir, cancel_event=None):
        # 模拟重算进行中：先 sleep 再检查取消，保证取消有机会在返回前到达
        for _ in range(100):
            await asyncio.sleep(0.05)
            if cancel_event is not None and cancel_event.is_set():
                cancelled_inside.set()
                raise manager_mod.RecalcCancelled("cancelled")
        return task_dir / "output" / "input.xlsx"

    monkeypatch.setattr(manager_mod, "run_recalc", fake_recalc)

    rows_info = [(2, "S001", None)]
    task_dir = tmp_path / "staging" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "input.xlsx").write_bytes(buf.getvalue())

    # 直接手工构造任务记录后启动协程（绕过上传校验）
    rec = {
        "task_id": "t1", "status": TaskStatus.QUEUED.value,
        "filename": "w.xlsx", "size_bytes": 4,
        "created_at": "now", "started_at": None, "finished_at": None,
        "error_code": None, "error_message": None,
        "cancel_requested": False, "samples": None, "summary": None,
    }
    mgr._tasks["t1"] = rec
    mgr._cancel_events["t1"] = asyncio.Event()
    mgr._persist(rec)
    job = asyncio.create_task(mgr._run("t1", task_dir, rows_info))

    await asyncio.sleep(0.15)  # 等它进入 running
    got, accepted = await mgr.request_cancel("t1")
    assert accepted is True
    await asyncio.wait_for(job, timeout=5)

    final = await mgr.get("t1")
    assert final["status"] == "cancelled"
    assert final["samples"] is None
    assert cancelled_inside.is_set()
    assert not task_dir.exists()
    assert mgr.slots.in_use == 0
