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
| 状态查询 | 只读，任何时候都可用，不受另一批取消/超时/回收影响 |
| 重启恢复 | 见下「重启恢复（异常退出后的回收）」 |

任务状态机：`queued → running → completed | failed | cancelled`；
重启后未完成：`非终态 → recovering → interrupted`。
取消、超时、失败、中断均**不发布**逐样本结果。

## 二.一、重启恢复（异常退出后的回收）

工作进程在重算中被强制结束（如 `kill -9`）后，LibreOffice 计算进程会
**成为孤儿继续运行**，甚至重建已清理的暂存输出。本服务按以下顺序回收，
**确认旧批真正停止后才上报状态**：

1. 启动时扫描持久化记录：非终态批次置为 `recovering`（可查、不可取消），
   并**继续占用重算名额**——遗留与新批合计始终不超过 2 批；
2. 后台回收协程按**该批独有的暂存路径标记**（`-env:UserInstallation`、
   `--outdir` 等命令行参数包含 `staging/<task_id>/`）在 `/proc` 中精确
   匹配残留进程，结合记录中的 `recalc_pid`（杀前复核命令行，防 PID 复用），
   先 SIGTERM 宽限、再 SIGKILL，**整组终止**计算进程及其子进程；
   只按任务独有路径匹配，**不会误停同机无关进程或其它批次**；
3. 确认进程退出后清理暂存，并复核**无进程复活、无目录重建**；
4. 全部确认后才落 `interrupted`（`INTERRUPTED_BY_RESTART`）并**归还名额**，
   已释放名额立即可处理新批；
5. 回收全程**幂等**：回收途中再次退出，下次启动重新执行完整回收；
   极端情况下进程无法确认退出（如信号被环境拒绝），批次保持 `recovering`
   慢速重试，确认退出前绝不落终态、绝不释放名额。

旧批**不续算、不发布判定**：其工作协程已随旧进程消亡，恢复流程只负责收尾。

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
Docker volume `verdict-data`（`/data`）；`init: true` 让 tini 担任 PID 1，
回收工作进程异常退出后留下的孤儿进程僵尸。可用环境变量调整：
`RECALC_TIMEOUT_SECONDS`、`MAX_CONCURRENT_BATCHES`、`MAX_UPLOAD_BYTES`、
`MAX_SAMPLE_ROWS`、`DATA_DIR`、`SOFFICE_BIN`，以及回收节奏
`RECOVERY_TERM_GRACE_SECONDS`、`RECOVERY_KILL_TIMEOUT_SECONDS`、
`RECOVERY_MAX_ROUNDS`、`RECOVERY_SLOW_RETRY_INTERVAL_SECONDS`。

### 工作进程退出 → 重启回收演示

```bash
# Docker Compose：监督循环复现“同机原数据目录重启”，pkill -9 杀掉工作进程
scripts/demo_restart_recovery.sh docker

# 本地（需本机已有 soffice）：演示端点 os._exit 模拟工作进程被强制结束
scripts/demo_restart_recovery.sh
```

演示依次展示：上传样例进入重算 → 强制结束工作进程（孤儿计算进程仍在运行）
→ 原数据目录重启 → 旧批 `recovering`（名额占用）→ 确认进程退出、暂存清除后
`interrupted`（名额归还）→ 暂存不复活 → 新批正常完成。

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

测试覆盖（40 个用例）：缓存缺失/一致/失真、样例文件、宏与外链拒收、
超限与结构校验、公式错误整批失败、运行中取消、排队取消、终态不可取消、
两批并发与名额释放、暂存清理、真实 LibreOffice 超时、重启中断标记；
异常退出回收：遗留进程组终止与暂存防复活、不误停无关进程/其它批次、
PID 复用防护、回收幂等、recovering 不可取消、名额占用与归还、
信号被拒时的慢速重试收尾（需真实信号的用例在 seccomp 受限环境自动跳过）。

## 七、目录结构

```
app/            FastAPI 应用
  config.py       配置（上限/超时/回收节奏/路径，环境变量可覆盖）
  models.py       状态枚举（含 recovering）与响应模型
  security.py     宏/外链/结构拒收校验（原件只读）
  workbook_io.py  openpyxl 读缓存、读重算结果、错误识别
  recalc.py       LibreOffice 子进程、独立 profile/进程组、超时与取消、
                  遗留进程发现（/proc 标记匹配）与整组回收
  manager.py      名额控制、状态机、持久化、重启恢复与后台回收
  main.py         HTTP 入口
scripts/        失真样例生成器、重启回收演示脚本、演示服务
samples/        已生成的缓存失真样例
tests/          pytest 测试套件
Dockerfile / docker-compose.yml / docker-compose.demo.yml（回收演示覆盖）
```
