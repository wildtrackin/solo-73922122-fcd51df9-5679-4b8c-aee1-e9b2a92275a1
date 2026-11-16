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
) -> Path:
    """执行一次 headless 转换重算。

    task_dir 下需已有 input.xlsx；输出到 task_dir/output/input.xlsx。
    超时或取消时杀掉整个进程组并抛出对应异常。
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
