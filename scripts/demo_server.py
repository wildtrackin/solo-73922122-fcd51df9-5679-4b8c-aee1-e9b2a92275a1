"""演示专用：在真实应用上额外挂一个「模拟工作进程被强制结束」的端点。

POST /__demo_crash 调用 os._exit(137) 让 API 工作进程立即退出
（不经过信号、不做任何清理），效果等同 kill -9：
正在重算的 LibreOffice 子进程被遗弃，继续运行。
仅用于 scripts/demo_restart_recovery.sh 的本地演示，不参与正常服务。
"""

from __future__ import annotations

import os

import uvicorn

from app.main import app


@app.post("/__demo_crash", include_in_schema=False)
async def _demo_crash() -> None:
    # 立即终止本进程：模拟工作进程在重算中被强制结束
    os._exit(137)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
