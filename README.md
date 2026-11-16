# 检测工作簿 PASS/FAIL 缓存核对服务

实验室数据员收到检测工作簿后，需要确认工作簿里**缓存的 PASS/FAIL 判定**，
在去掉缓存、用公式**实际重算**之后是否发生变化（典型风险：标定表参数已改，
但文件没有在 Excel/WPS 里重新打开保存，缓存仍是旧结论）。本服务用
FastAPI + openpyxl + LibreOffice（headless）完成自动核对，决定工作簿能否入仓。

## 一、核对规则

- 仅接收 **≤ 2 MiB 的 `.xlsx`**；样本表名固定为 `样本表`：
  - A 列：唯一样本号（不能为空、不能重复）；
  - B 列：判定**公式**，结果必须是 `PASS` 或 `FAIL`，可引用 `标定表` 等其它工作表；
  - 数据行最多 **100 行**（第 1 行为表头）。
- **原件不修改**：上传字节先做只读校验，再写入独立暂存目录；重算在副本上进行。
- 逐样本返回三要素：
  - 原缓存判定（`cached_verdict`：`PASS`/`FAIL`，缺缓存记为 `null` 即“未知”）；
  - 实际重算判定（`recalculated_verdict`）；
  - 是否变化（`changed`）及变化类型（`change_type`）：
    `unchanged` / `pass_to_fail` / `fail_to_pass` / `cache_unknown`。
- **公式报错（`#DIV/0!`、`#REF!`、`#N/A` 等）→ 整批失败**，不发布逐样本结果。
- **拒收宏与外链**（HTTP 422，不创建任务）：
  - 宏：`vbaProject.bin`、`.xlsm/.xltm/.xlam`、Content_Types 宏声明；
  - 外链：`externalLinks/`、`externalConnections`、关系部件 `TargetMode="External"`、
    公式中出现 `[其它工作簿.xlsx]`、盘符路径、UNC 路径。

## 二、任务与并发语义

| 能力 | 行为 |
| --- | --- |
| 并发 | 最多 **2 批同时重算**，超出的任务在队列中等待（状态 `queued`） |
| 单批时限 | LibreOffice 重算 **10 秒**，超时判 `failed`（`RECALC_TIMEOUT`），不发布结果 |
| 取消 | 立即受理；排队任务立即落 `cancelled`；运行中任务通知协程后，**等计算进程及子进程退出、暂存清理完毕**才落 `cancelled` 并释放名额 |
| 终态保护 | 已完成/失败/取消/中断的任务再次取消 → 告知“不可取消”，状态不变 |
| 隔离 | 每批独立暂存目录、独立 LibreOffice profile、独立进程组、精简环境变量；工作簿/进程/结果互不影响 |
| 状态查询 | 只读，任何时候都可用，不受另一批取消/超时影响 |
| 重启恢复 | 启动时把未走完终态的持久化任务标记为 `interrupted`，清理无主暂存 |

任务状态机：`queued → running → completed | failed | cancelled`；
重启后未完成 → `interrupted`。取消、超时、失败均**不发布**逐样本结果。

## 三、API（启动后访问 `/docs` 查看交互式文档）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/tasks` | multipart 上传 xlsx，返回 `task_id`（202） |
| `GET` | `/tasks/{task_id}` | 按任务号查询状态、错误与逐样本核对结果 |
| `POST` | `/tasks/{task_id}/cancel` | 取消任务；终态任务返回“不可取消” |
| `GET` | `/tasks` | 列出全部任务 |
| `GET` | `/health` | 健康检查与名额使用情况 |

主要错误码：`FILE_TOO_LARGE(413)`、`BAD_EXTENSION(400)`、`INVALID_XLSX`、
`MACRO_NOT_ALLOWED`、`EXTERNAL_LINK_NOT_ALLOWED`、`SHEET_NOT_FOUND`、
`DUPLICATE_SAMPLE_ID`、`VERDICT_NOT_FORMULA`、`TOO_MANY_ROWS`、
`EMPTY_SAMPLE_ID`、`FORMULA_ERROR`、`RECALC_TIMEOUT`、`RECALC_FAILED`、
`UNEXPECTED_VERDICT`、`INTERRUPTED_BY_RESTART`、`TASK_NOT_FOUND(404)`。

## 四、运行方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，文档 http://localhost:8000/docs
```

容器内以非 root 用户运行，LibreOffice 通过 apt 安装，任务数据持久化在
Docker volume `verdict-data`（`/data`）。可用环境变量调整：
`RECALC_TIMEOUT_SECONDS`、`MAX_CONCURRENT_BATCHES`、`MAX_UPLOAD_BYTES`、
`MAX_SAMPLE_ROWS`、`DATA_DIR`、`SOFFICE_BIN`。

### 本地直接运行（需本机已有 soffice）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 快速手工验证

```bash
# 1. 上传缓存失真样例
curl -X POST http://localhost:8000/tasks \
  -F "file=@samples/cache_drift_sample.xlsx;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 2. 用返回的 task_id 查询，可见 S004/S005：cached=PASS → recalculated=FAIL
curl http://localhost:8000/tasks/<task_id>
```

## 五、缓存失真样例

`samples/cache_drift_sample.xlsx` 由 `scripts/make_sample.py` 生成，可复现：

1. openpyxl 建簿：`标定表!B2=12` 为合格阈值，样本表 B 列
   `=IF(C行>=标定表!$B$2,"PASS","FAIL")`；
2. LibreOffice 强制重算，写入真实缓存（S004=9.8、S005=3.2 实为 FAIL）；
3. 直接改写 sheet XML，把这两个单元格的缓存值伪造成 `PASS`（公式不动），
   模拟“参数已变、缓存未刷新”。

重新生成（需要 soffice）：

```bash
SOFFICE_BIN=$(which soffice) python scripts/make_sample.py samples
```

## 六、测试

```bash
# 容器内（soffice 在 PATH）：
pytest -q
# 本机便携版 LibreOffice：
SOFFICE_BIN=/path/to/soffice SOFFICE_LD_LIBRARY_PATH=/path/to/libs pytest -q
```

测试覆盖（25 个用例）：缓存缺失/一致/失真、样例文件、宏与外链拒收、
超限与结构校验、公式错误整批失败、运行中取消、排队取消、终态不可取消、
两批并发与名额释放、暂存清理、真实 LibreOffice 超时、重启中断标记。

## 七、目录结构

```
app/            FastAPI 应用
  config.py       配置（上限/超时/路径，环境变量可覆盖）
  models.py       状态枚举与响应模型
  security.py     宏/外链/结构拒收校验（原件只读）
  workbook_io.py  openpyxl 读缓存、读重算结果、错误识别
  recalc.py       LibreOffice 子进程、独立 profile/进程组、超时与取消
  manager.py      名额控制、状态机、持久化与重启恢复
  main.py         HTTP 入口
scripts/        失真样例生成器
samples/        已生成的缓存失真样例
tests/          pytest 测试套件
Dockerfile / docker-compose.yml
```
