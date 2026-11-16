"""全局配置，全部可通过环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 上传工作簿大小上限：2 MiB
MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
# 样本表最多 100 行数据（不含表头）
MAX_SAMPLE_ROWS = _env_int("MAX_SAMPLE_ROWS", 100)
# 同时重算的批次上限
MAX_CONCURRENT_BATCHES = _env_int("MAX_CONCURRENT_BATCHES", 2)
# 单批 LibreOffice 重算超时（秒）
RECALC_TIMEOUT_SECONDS = _env_int("RECALC_TIMEOUT_SECONDS", 10)
# 轮询子进程的间隔
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "0.2"))

# 样本表名称与关键列
SAMPLE_SHEET_NAME = os.environ.get("SAMPLE_SHEET_NAME", "样本表")
SAMPLE_ID_COLUMN = 1  # A 列：唯一样本号
VERDICT_COLUMN = 2  # B 列：判定公式
HEADER_ROWS = 1  # 第一行表头

# LibreOffice 可执行文件：优先使用环境变量，其次 PATH
SOFFICE_BIN = os.environ.get("SOFFICE_BIN", "soffice")

# 运行时数据目录（任务暂存、持久化任务记录）
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STAGING_DIR = DATA_DIR / "staging"
TASKS_DIR = DATA_DIR / "tasks"

# 允许的文件扩展名与内容类型
ALLOWED_SUFFIX = ".xlsx"
ALLOWED_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # 部分浏览器/客户端上传 xlsx 时使用通用类型
    "application/octet-stream",
}
