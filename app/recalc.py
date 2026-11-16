"""LibreOffice headless 重算。

隔离措施（每批互不影响）：
- 独立暂存目录、独立 UserInstallation profile；
- 独立进程组（os.setsid），取消/超时时 kill 整个进程组，含 soffice 子进程；
- 精简后的干净环境变量，不继承父进程的可疑变量；
- 每个 profile 内置 registrymodifications.xcu，强制 OOXML 载入即重算，
  并把宏安全级别调到最高（Very High，不执行任何宏）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path

from . import config

# 强制重算 + 宏全禁用的 profile 配置
REGISTRY_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
  <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
  <prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop>
 </item>
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
 </item>
</oor:items>
"""


class RecalcTimeout(Exception):
    """重算超过单批时限。"""


class RecalcCancelled(Exception):
    """重算过程中收到取消。"""


class RecalcFailed(Exception):
    """LibreOffice 进程异常退出或未产出文件。"""


def prepare_profile(profile_dir: Path) -> Path:
    user_dir = profile_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "registrymodifications.xcu").write_text(
        REGISTRY_XCU, encoding="utf-8"
    )
    return profile_dir


def _build_env() -> dict[str, str]:
    """干净的子进程环境，仅保留运行必需变量。"""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    # 本地便携版 LibreOffice 需要 LD_LIBRARY_PATH（容器内 apt 安装不需要）
    extra = os.environ.get("SOFFICE_LD_LIBRARY_PATH")
    if extra:
        env["LD_LIBRARY_PATH"] = extra
    return env


async def run_recalc(
    task_dir: Path,
    input_name: str = "input.xlsx",
    cancel_event: asyncio.Event | None = None,
    on_process_start=None,
) -> Path:
    """执行一次 headless 转换重算。

    task_dir 下需已有 input.xlsx；输出到 task_dir/output/input.xlsx。
    超时或取消时杀掉整个进程组并抛出对应异常。
    on_process_start：可选异步回调，子进程启动后以 PID 调用，
    供编排层把计算进程身份持久化（异常退出后的重启回收要用）。
    """
    input_path = task_dir / input_name
    output_dir = task_dir / "output"
    profile_dir = task_dir / "lo-profile"
    output_dir.mkdir(parents=True, exist_ok=True)
    prepare_profile(profile_dir)

    cmd = [
        config.SOFFICE_BIN,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--invisible",
        f"-env:UserInstallation=file://{profile_dir.resolve()}",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(output_dir.resolve()),
        str(input_path.resolve()),
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(task_dir),
        env=_build_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # 独立进程组，便于整组 kill
    )
    if on_process_start is not None:
        # 先回报 PID 再进入监视：即使此刻服务被杀，重启后也能按记录找到它
        await on_process_start(process.pid)

    deadline = asyncio.get_running_loop().time() + config.RECALC_TIMEOUT_SECONDS

    async def _watch() -> None:
        """轮询进程退出、取消事件与超时。

        整体包在 shield 中：外部协程被取消时，这里仍能完成进程组回收，
        再由调用方根据 cancel_event 转成 RecalcCancelled。
        """
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill_process_group(process)
                await process.wait()
                raise RecalcCancelled("任务已取消")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                _kill_process_group(process)
                await process.wait()
                raise RecalcTimeout(
                    f"重算超过 {config.RECALC_TIMEOUT_SECONDS} 秒"
                )
            try:
                # 外层已用 shield 包裹；这里只做带超时的退出等待
                await asyncio.wait_for(
                    process.wait(),
                    timeout=min(config.POLL_INTERVAL_SECONDS, remaining),
                )
                return  # 进程正常退出
            except asyncio.TimeoutError:
                continue

    try:
        await asyncio.shield(_watch())
    except asyncio.CancelledError:
        # 外层任务被取消（理论上取消走 event；这里做防御性兜底）
        if cancel_event is not None:
            cancel_event.set()
        _kill_process_group(process)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=3)
        except asyncio.TimeoutError:
            pass
        raise RecalcCancelled("任务已取消")
    except (RecalcTimeout, RecalcCancelled):
        raise
    finally:
        # 无论成功与否，确保进程已被回收；残留则强杀
        if process.returncode is None:
            _kill_process_group(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    if process.returncode != 0:
        stderr = await _drain_stream(process.stderr)
        raise RecalcFailed(
            f"LibreOffice 退出码 {process.returncode}：{stderr[:300]}"
        )

    # soffice 即使 rc=0 也可能在输出阶段静默失败（如输入输出同路径），
    # 检查 stdout/stderr 中的 Error 标记
    stdout = await _drain_stream(process.stdout)
    stderr = await _drain_stream(process.stderr)
    combined = f"{stdout}\n{stderr}"
    if "Error:" in combined or "failed:" in combined.lower():
        raise RecalcFailed(f"LibreOffice 转换报错：{combined.strip()[:300]}")

    out_file = output_dir / input_name
    if not out_file.exists():
        raise RecalcFailed(f"未找到重算输出文件，soffice 输出：{stdout[:300]}")
    return out_file


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其全部子孙进程（同一进程组）。

    EPERM 在两种情况下出现：进程已退出（被回收），或运行环境不授予
    CAP_KILL（受限沙箱）。前者无需处理；后者退化为直接 kill 子进程，
    最终仍由调用方的 process.wait() 回收。
    """
    if process.returncode is not None:
        return
    pgid: int | None = None
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        pgid = None
    except Exception:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        return

    if pgid is None:
        # killpg 被环境拒绝，退化为直接终止子进程
        try:
            process.terminate()
        except (ProcessLookupError, PermissionError):
            return
        return

    # 给 SIGTERM 一点宽限，仍存活则 SIGKILL
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        # 无法探测进程组：交给 wait 兜底
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass


# ---------- 异常退出后的遗留进程回收（重启恢复用） ----------
#
# 身份识别：每批重算进程的命令行都带有该批独有的暂存路径
# （-env:UserInstallation=file://<staging>/<task_id>/lo-profile、
#   --outdir <staging>/<task_id>/output、输入文件路径）。
# task_id 是 32 位十六进制随机串，其它批次、同机无关进程（包括用户自己
# 开的 soffice）的命令行不可能包含该路径，因此按此标记匹配不会误杀。
# 记录的 recalc_pid 仅作为线索，杀前必须用同一标记复核，防止 PID 被
# 系统复用后误杀无关进程。


def _proc_root() -> Path | None:
    root = Path("/proc")
    return root if root.is_dir() else None


def _task_marker(task_id: str) -> bytes:
    """该批重算进程命令行中必然出现的独有字节串。"""
    return str((config.STAGING_DIR / task_id).resolve()).encode()


def find_task_processes(task_id: str) -> list[int]:
    """扫描 /proc，返回命令行引用该批暂存路径的存活进程 PID。

    僵尸进程 cmdline 为空，天然被排除；非 Linux（无 /proc）退化为空表。
    """
    root = _proc_root()
    if root is None:
        return []
    marker = _task_marker(task_id)
    hits: list[int] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker in cmdline:
            hits.append(int(entry.name))
    return hits


def pid_belongs_to_task(pid: int, task_id: str) -> bool:
    """复核某个 PID 当前确属该批的计算进程（防 PID 复用误杀）。"""
    root = _proc_root()
    if root is None:
        return False
    try:
        cmdline = (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return _task_marker(task_id) in cmdline


def _pid_alive(pid: int) -> bool:
    """进程存在且非僵尸。/proc 不可读时按已退出处理（回收继续推进）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    try:
        # comm 字段可含空格与括号，状态是最后一个 ')' 之后的第一列
        state = stat.rsplit(")", 1)[1].split()[0]
    except IndexError:
        return False
    return state != "Z"


async def _wait_all_exit(pids, timeout: float) -> list[int]:
    """轮询等待全部 PID 退出，返回超时后仍存活的列表。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    remaining = [p for p in set(pids) if _pid_alive(p)]
    while remaining and loop.time() < deadline:
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        remaining = [p for p in remaining if _pid_alive(p)]
    return remaining


def _signal_targets(pids, sig: int) -> None:
    """按进程组发信号（一组信号即可覆盖 fork 出的全部子孙）。

    取不到进程组时退化为单进程信号。绝不向 0/1/自身所在进程组发信号，
    避免误伤服务自身或系统进程。
    """
    own_pgid = os.getpgrp()
    for pid in pids:
        pgid: int | None
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        if pgid is not None and pgid not in (0, 1, own_pgid):
            try:
                os.killpg(pgid, sig)
                continue
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def _collect_targets(task_id: str, extra_pids) -> list[int]:
    """汇总回收目标：/proc 扫描命中 + 复核通过的记录 PID。"""
    pids = set(find_task_processes(task_id))
    for pid in extra_pids:
        if pid and _pid_alive(pid) and pid_belongs_to_task(pid, task_id):
            pids.add(pid)
    return sorted(pids)


async def reap_task_processes(
    task_id: str,
    known_pids=(),
    term_grace: float | None = None,
    kill_timeout: float | None = None,
) -> list[int]:
    """终止某批次遗留的计算进程（含子进程）并等待退出。

    只匹配命令行含该批独有暂存路径的进程，不会误停同机无关进程或
    其它批次。先 SIGTERM 宽限，再 SIGKILL；返回最终仍存活的 PID
    （正常为空，调用方可据此决定是否重试）。
    """
    if term_grace is None:
        term_grace = config.RECOVERY_TERM_GRACE_SECONDS
    if kill_timeout is None:
        kill_timeout = config.RECOVERY_KILL_TIMEOUT_SECONDS

    targets = await _collect_targets(task_id, known_pids)
    if not targets:
        return []
    _signal_targets(targets, signal.SIGTERM)
    remaining = await _wait_all_exit(targets, term_grace)
    if remaining:
        # 宽限内未退出：重新扫描（含其间 fork 的新子进程），升级 SIGKILL
        targets = await _collect_targets(task_id, remaining)
        _signal_targets(targets, signal.SIGKILL)
        remaining = await _wait_all_exit(targets, kill_timeout)
    if not remaining:
        # 兜底复核：扫描与信号之间可能 fork 出的新进程
        remaining = await _collect_targets(task_id, [])
        if remaining:
            _signal_targets(remaining, signal.SIGKILL)
            remaining = await _wait_all_exit(remaining, kill_timeout)
    return remaining


async def _drain_stream(stream) -> str:
    if stream is None:
        return ""
    try:
        data = await stream.read()
        return data.decode("utf-8", "ignore")
    except Exception:
        return ""


def _on_rm_error(func, path, exc_info):
    """shutil.rmtree 的 onerror（Python ≤3.11）：只读文件改权限后重试。"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except (PermissionError, FileNotFoundError, OSError):
        # 最后兜底：环境不允许删除时不阻断终态流转
        pass


def _on_rm_error_312(func, path, exc):
    """shutil.rmtree 的 onexc（Python ≥3.12）。"""
    _on_rm_error(func, path, None)


def cleanup_task_dir(task_dir: Path) -> None:
    """任务终态后清理暂存（原件、profile、输出全部删除）。"""
    if not task_dir.exists():
        return
    # 子进程可能留下只读文件，先恢复写权限再删除
    for root, dirs, files in os.walk(task_dir):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except (PermissionError, FileNotFoundError):
                pass
    # Python 3.12 起 onerror 改名为 onexc
    if sys.version_info >= (3, 12):
        shutil.rmtree(task_dir, onexc=_on_rm_error_312)
    else:
        shutil.rmtree(task_dir, onerror=_on_rm_error)
