# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 再生水循环用量核算

`/api/recycled-water` 模块面向五套再生水处理系统的回用量台账，自然日按北京时间（UTC+08:00）切分：

- `PUT /sources`、`GET /sources`：登记处理系统及单周期水量/周期时长上限，超上限读数自动进入待核查。
- `POST /readings`：上报带来源、水系、处理环节和计量周期的读数；`client_ref` 幂等去重，跨午夜周期按秒均摊到各自然日。响应中的 `allocations` 给出每日分摊量。
- 读数状态机：`accepted` / `flagged` / `rejected` / `withdrawn`。异常读数只进待核查，不会被悄悄排除；`/readings/{id}/review` 确认或驳回，`/withdraw`、`/reinstate`、`/reopen` 支持维护期回退、补录恢复和驳回复核，全部流转写入 `events`。
- `POST /reports/{day}/issue` 签发日报；已签发版本不可变，迟到补录或核查决定导致变化时，先经 `GET /reports/{day}/diff` 查看差异，再带 `confirm=true` 生成新版本。每个版本快照全部构成读数（含待核查/驳回）及其当日签入量。
- `GET /reports/{day}`、`/versions`、`/versions/{v}`：查看当前版本、历史版本和作废留档；`outdated` 提示当前读数已偏离签发版本。
- `GET /summary?from=YYYY-MM-DD&to=YYYY-MM-DD`：区间汇总，每个数字的 `sources` 直接列出由哪些读数（来源系统、状态、整周期量、当日签入量）构成。

所有读数、核查决定、日报版本与组件溯源均保存在 SQLite，服务重启后保持不变。`tests/test_recycled_water.py` 覆盖重复上报、跨日边界、核查流转、版本签发与重启持久化。
