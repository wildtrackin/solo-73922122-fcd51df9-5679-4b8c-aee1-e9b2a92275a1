"""异常退出后的回收：遗留进程终止、暂存防复活、不误伤、幂等与名额。

用带「任务暂存路径标记」的真实子进程模拟被遗弃的 LibreOffice 计算进程，
验证重启恢复只回收该批进程及其子进程，确认退出并清暂存后才落 interrupted。

注：部分用例需要真实的信号能力（SIGTERM/SIGKILL）。若运行环境以
seccomp 拦截信号系统调用（如受限沙箱），这些用例自动跳过；
其余用例（含信号被拒时的慢速重试收尾）在任何环境都会执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest

# 假计算进程：自带一个无标记子进程（考验进程组回收），
# 并不停重建暂存输出（模拟 soffice 重建已清掉的输出目录）
FAKE_CALC_SCRIPT = (
    "import os, sys, time, subprocess\n"
    "subprocess.Popen(['sleep', '30'])\n"
    "out = sys.argv[1]\n"
    "deadline = time.time() + 30\n"
    "while time.time() < deadline:\n"
    "    os.makedirs(out, exist_ok=True)\n"
    "    open(os.path.join(out, 'out.xlsx'), 'w').write('x')\n"
    "    time.sleep(0.2)\n"
)


def _signals_blocked() -> bool:
    """探测运行环境是否拦截信号系统调用（受限沙箱）。"""
    probe = subprocess.Popen(["sleep", "3"])
    try:
        probe.send_signal(signal.SIGTERM)
    except PermissionError:
        return True  # 探针进程数秒后自行退出
    try:
        probe.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return False


SIGNALS_BLOCKED = _signals_blocked()
requires_signals = pytest.mark.skipif(
    SIGNALS_BLOCKED,
    reason="当前环境拦截信号系统调用，无法验证主动终止路径",
)


def _pid_alive(pid: int) -> bool:
    from app.recalc import _pid_alive as alive
    return alive(pid)


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _kill_quiet(*procs) -> None:
    """测试收尾清理：信号不可用的环境下静默跳过（进程短寿，自行退出）。"""
    for p in procs:
        try:
            p.kill()
        except (PermissionError, ProcessLookupError, OSError):
            pass


def _child_pids(pid: int) -> list[int]:
    try:
        text = open(f"/proc/{pid}/task/{pid}/children").read().split()
        return [int(t) for t in text]
    except OSError:
        return []


def _write_record(data_dir, task_id: str, status: str, **extra) -> dict:
    record = {
        "task_id": task_id,
        "status": status,
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
    record.update(extra)
    (data_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (data_dir / "tasks" / f"{task_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record


def _patch_dirs(monkeypatch, data_dir):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "TASKS_DIR", data_dir / "tasks")
    monkeypatch.setattr(config, "STAGING_DIR", data_dir / "staging")
    (data_dir / "staging").mkdir(parents=True, exist_ok=True)


def _fast_recovery(monkeypatch):
    """把回收节奏调快，便于测试。"""
    from app import config
    monkeypatch.setattr(config, "RECOVERY_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(config, "RECOVERY_KILL_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(config, "RECOVERY_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "RECOVERY_SLOW_RETRY_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "RECOVERY_VERIFY_DELAY_SECONDS", 0.05)


async def _run_reclaim(mgr) -> None:
    mgr.start_reclamation()
    if mgr._reclaim_jobs:
        await asyncio.gather(*list(mgr._reclaim_jobs.values()))


# ---------- 真实信号：主动终止路径 ----------


@pytest.mark.asyncio
@requires_signals
async def test_reclaim_kills_orphan_process_group(tmp_path, monkeypatch):
    """核心缺陷复现：旧批进程仍运行且会重建暂存输出。

    回收必须终止其进程组（含无标记子进程）、清掉暂存且不复活，
    然后才落 interrupted 并归还名额。
    """
    from app.recalc import find_task_processes

    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    task_id = "orphan001"
    staging = data_dir / "staging" / task_id
    staging.mkdir(parents=True)
    (staging / "input.xlsx").write_bytes(b"x")

    # 模拟被遗弃的计算进程：独立会话/进程组，命令行带该批暂存路径
    proc = subprocess.Popen(
        [sys.executable, "-c", FAKE_CALC_SCRIPT, str(staging / "output")],
        start_new_session=True,
    )
    await asyncio.sleep(0.5)  # 让它 fork 出子进程并开始重建输出
    children = _child_pids(proc.pid)
    assert children, "假计算进程应已 fork 出子进程"
    assert (staging / "output" / "out.xlsx").exists()
    assert find_task_processes(task_id) == [proc.pid]

    _write_record(data_dir, task_id, "running", recalc_pid=proc.pid)

    from app.manager import TaskManager
    mgr = TaskManager()
    assert mgr.recover_on_startup() == [task_id]
    assert mgr._tasks[task_id]["status"] == "recovering"
    assert mgr.slots.in_use == 1
    # 同步恢复阶段不杀进程：假进程仍在运行（回收是后台异步的）
    assert _pid_alive(proc.pid)

    await _run_reclaim(mgr)

    # 计算进程及其无标记子进程均已退出（进程组回收）
    assert _wait_dead(proc.pid), "遗留计算进程未被终止"
    assert _wait_dead(children[0]), "遗留进程的子进程未被一并终止"
    # 暂存已清除且不再复活（进程已死，无人能重建输出）
    assert not staging.exists()
    await asyncio.sleep(0.6)
    assert not staging.exists(), "暂存目录被残留进程重建（复活）"
    # 落 interrupted、不发布结果、名额归还
    final = mgr._tasks[task_id]
    assert final["status"] == "interrupted"
    assert final["error_code"] == "INTERRUPTED_BY_RESTART"
    assert final["samples"] is None
    assert final["recalc_pid"] is None
    assert mgr.slots.in_use == 0


@pytest.mark.asyncio
@requires_signals
async def test_reclaim_ignores_unrelated_and_other_batch(tmp_path, monkeypatch):
    """不得误停：同机无关进程、其它批次进程、PID 复用的无关进程。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    old_id = "oldbatch"
    other_id = "newbatch"
    (data_dir / "staging" / old_id).mkdir(parents=True)

    # 同机无关进程：命令行不含任何暂存路径
    unrelated = subprocess.Popen(["sleep", "20"])
    # 另一批次（新批）的计算进程：带 other_id 的暂存路径标记
    other_dir = data_dir / "staging" / other_id
    other_dir.mkdir(parents=True)
    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)",
         f"--profile={other_dir}/lo-profile"],
        start_new_session=True,
    )
    try:
        # 记录里的 recalc_pid 指向无关进程（模拟 PID 复用）：也不得误杀
        _write_record(data_dir, old_id, "running", recalc_pid=unrelated.pid)

        from app.manager import TaskManager
        mgr = TaskManager()
        mgr.recover_on_startup()
        await _run_reclaim(mgr)

        assert mgr._tasks[old_id]["status"] == "interrupted"
        assert _pid_alive(unrelated.pid), "同机无关进程被误停"
        assert _pid_alive(other.pid), "其它批次的计算进程被误停"
        assert other_dir.exists(), "其它批次的暂存被误清"
    finally:
        _kill_quiet(unrelated, other)


# ---------- 任何环境可跑：发现、复核、幂等、名额、慢速收尾 ----------


@pytest.mark.asyncio
async def test_recovery_idempotent_after_crash_mid_reclaim(
    tmp_path, monkeypatch
):
    """回收途中再次退出：recovering 记录下次启动重新回收，仍能收尾。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    task_id = "midcrash"
    staging = data_dir / "staging" / task_id
    staging.mkdir(parents=True)
    (staging / "input.xlsx").write_bytes(b"x")
    # 上次回收途中崩溃：状态停在 recovering，暂存尚未清完
    _write_record(data_dir, task_id, "recovering")

    from app.manager import TaskManager
    mgr = TaskManager()
    assert mgr.recover_on_startup() == [task_id]
    assert mgr._tasks[task_id]["status"] == "recovering"
    assert mgr.slots.in_use == 1

    await _run_reclaim(mgr)
    assert mgr._tasks[task_id]["status"] == "interrupted"
    assert not staging.exists()
    assert mgr.slots.in_use == 0

    # 再次重启（interrupted 已是终态）：幂等，不再重复回收
    mgr2 = TaskManager()
    assert mgr2.recover_on_startup() == []
    assert mgr2.slots.in_use == 0


@pytest.mark.asyncio
async def test_orphan_staging_dir_removed_immediately(tmp_path, monkeypatch):
    """无任务记录的暂存目录不可能有进程（记录先于进程落盘），直接清理。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    orphan = data_dir / "staging" / "norecord"
    orphan.mkdir(parents=True)
    (orphan / "input.xlsx").write_bytes(b"x")

    from app.manager import TaskManager
    mgr = TaskManager()
    assert mgr.recover_on_startup() == []
    assert not orphan.exists()
    assert mgr.slots.in_use == 0


@pytest.mark.asyncio
async def test_recovering_task_not_cancellable(tmp_path, monkeypatch):
    """recovering 即将落 interrupted，不受理取消，状态不被打乱。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    task_id = "recovering1"
    _write_record(data_dir, task_id, "running")

    from app.manager import TaskManager
    mgr = TaskManager()
    mgr.recover_on_startup()
    record, accepted = await mgr.request_cancel(task_id)
    assert accepted is False
    assert record["status"] == "recovering"
    assert mgr._tasks[task_id]["status"] == "recovering"

    await _run_reclaim(mgr)
    # 回收后已是终态，取消依旧不受理
    _record, accepted = await mgr.request_cancel(task_id)
    assert accepted is False
    assert mgr._tasks[task_id]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_slots_held_during_recovery_then_released(tmp_path, monkeypatch):
    """遗留批次占满名额时新批等待；每回收完一个就释放一个名额。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    _write_record(data_dir, "held1", "running")
    _write_record(data_dir, "held2", "queued")

    from app.manager import TaskManager
    mgr = TaskManager()
    assert mgr.recover_on_startup() == ["held1", "held2"]
    assert mgr.slots.in_use == 2

    # 名额被遗留批次占满：新任务暂时拿不到名额
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mgr.slots.acquire(), timeout=0.3)

    # 后台回收完成后名额归还，新批即可使用（遗留+新批合计不超上限）
    mgr.start_reclamation()
    await asyncio.wait_for(mgr.slots.acquire(), timeout=10)
    await asyncio.gather(*list(mgr._reclaim_jobs.values()))
    await mgr.slots.release()
    assert mgr.slots.in_use == 0
    assert mgr._tasks["held1"]["status"] == "interrupted"
    assert mgr._tasks["held2"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_reclaim_waits_for_natural_exit(tmp_path, monkeypatch):
    """进程未被立即终止时：保持 recovering 重试，确认退出后才落 interrupted。

    信号可用环境：快速轮直接终止；信号被拒环境：慢速轮等其自然退出。
    两种环境下最终都必须确认进程退出、暂存清除后才落终态。
    """
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    _fast_recovery(monkeypatch)
    task_id = "naturalexit"
    staging = data_dir / "staging" / task_id
    staging.mkdir(parents=True)
    (staging / "input.xlsx").write_bytes(b"x")
    # 短寿命“遗留进程”：约 2 秒后自行退出
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)",
         f"--outdir={staging}/output"],
        start_new_session=True,
    )
    _write_record(data_dir, task_id, "running", recalc_pid=proc.pid)

    from app.manager import TaskManager
    mgr = TaskManager()
    mgr.recover_on_startup()
    mgr.start_reclamation()
    await asyncio.sleep(0.4)
    if _pid_alive(proc.pid):
        # 进程还活着（如信号被拒）：必须仍是 recovering，名额不得释放
        assert mgr._tasks[task_id]["status"] == "recovering"
        assert mgr.slots.in_use == 1
    await asyncio.wait_for(
        asyncio.gather(*list(mgr._reclaim_jobs.values())), timeout=30
    )
    assert not _pid_alive(proc.pid)
    assert mgr._tasks[task_id]["status"] == "interrupted"
    assert not staging.exists()
    assert mgr.slots.in_use == 0


@pytest.mark.asyncio
async def test_pid_identity_guard(tmp_path, monkeypatch):
    """pid_belongs_to_task：只认命令行带该批暂存路径的进程。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    from app.recalc import pid_belongs_to_task

    plain = subprocess.Popen(["sleep", "10"])
    marked_dir = data_dir / "staging" / "markedtask"
    marked_dir.mkdir(parents=True)
    marked = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)",
         f"--outdir={marked_dir}/output"],
    )
    try:
        await asyncio.sleep(0.2)  # 等 exec 完成，/proc 命令行就绪
        assert pid_belongs_to_task(marked.pid, "markedtask") is True
        assert pid_belongs_to_task(plain.pid, "markedtask") is False
        assert pid_belongs_to_task(marked.pid, "othertask") is False
        assert pid_belongs_to_task(99999999, "markedtask") is False
    finally:
        _kill_quiet(plain, marked)


@pytest.mark.asyncio
async def test_find_task_processes_scopes_to_task(tmp_path, monkeypatch):
    """find_task_processes：只命中该批标记，不碰无关进程与本进程。"""
    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    from app.recalc import find_task_processes

    marked_dir = data_dir / "staging" / "scantask"
    marked_dir.mkdir(parents=True)
    marked = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)",
         f"--profile={marked_dir}/lo-profile"],
    )
    plain = subprocess.Popen(["sleep", "10"])
    try:
        await asyncio.sleep(0.2)
        hits = find_task_processes("scantask")
        assert hits == [marked.pid]
        assert plain.pid not in hits
        assert os.getpid() not in hits
        assert find_task_processes("nosuchtask") == []
    finally:
        _kill_quiet(marked, plain)


# ---------- 控制流（拦截信号系统调用，任何环境可跑） ----------


@pytest.mark.asyncio
async def test_reap_escalates_term_then_kill_by_group(tmp_path, monkeypatch):
    """不依赖真实信号：验证按进程组先 SIGTERM、超时升级 SIGKILL。"""
    import app.recalc as recalc_mod

    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    task_id = "escalation"
    (data_dir / "staging" / task_id).mkdir(parents=True)

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)",
         f"--x={data_dir}/staging/{task_id}"],
        start_new_session=True,
    )
    await asyncio.sleep(0.2)
    real_pgid = os.getpgid(proc.pid)
    calls: list[tuple] = []
    alive = {"v": True}

    def fake_killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))
        if sig == signal.SIGKILL:
            alive["v"] = False  # 模拟 SIGKILL 生效

    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(
        recalc_mod, "_pid_alive", lambda pid: alive["v"]
    )
    try:
        survivors = await recalc_mod.reap_task_processes(
            task_id, term_grace=0.2, kill_timeout=0.5
        )
        assert survivors == []
        # 先 SIGTERM 后 SIGKILL，且都发往该批进程组
        assert calls[0] == ("killpg", real_pgid, signal.SIGTERM)
        assert ("killpg", real_pgid, signal.SIGKILL) in calls
    finally:
        _kill_quiet(proc)


@pytest.mark.asyncio
async def test_reap_tolerates_signal_permission_denied(tmp_path, monkeypatch):
    """信号被环境拒绝（EPERM）时：回收不崩溃，报告未退出的幸存者。"""
    import app.recalc as recalc_mod

    data_dir = tmp_path / "data"
    _patch_dirs(monkeypatch, data_dir)
    task_id = "epermtask"
    (data_dir / "staging" / task_id).mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)",
         f"--x={data_dir}/staging/{task_id}"],
        start_new_session=True,
    )
    await asyncio.sleep(0.2)

    def denied(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(os, "kill", denied)
    try:
        survivors = await recalc_mod.reap_task_processes(
            task_id, term_grace=0.2, kill_timeout=0.3
        )
        assert proc.pid in survivors
        assert _pid_alive(proc.pid), "EPERM 下进程不应消失"
    finally:
        _kill_quiet(proc)


def test_signal_targets_never_touches_own_group(monkeypatch):
    """目标解析到本进程组时绝不用 killpg（防误停服务自身）。"""
    from app.recalc import _signal_targets

    calls: list[tuple] = []
    monkeypatch.setattr(
        os, "killpg", lambda p, s: calls.append(("killpg", p, s))
    )
    monkeypatch.setattr(
        os, "kill", lambda p, s: calls.append(("kill", p, s))
    )
    monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
    _signal_targets([123450], signal.SIGTERM)
    assert ("kill", 123450, signal.SIGTERM) in calls
    assert not any(c[0] == "killpg" for c in calls), "本进程组收到 killpg"
