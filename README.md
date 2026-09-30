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

## 循环用量核算

公园运维的五套再生水处理系统通过 `/api/recycled-water` 接口上报带 `source_system`（来源系统）和计量周期（`period_start`/`period_end`）的读数，服务按水系（`water_body`）、处理环节（`process_stage`）和自然日生成可复核的用量链：

- 周期不允许跨越自然日边界，跨日读数需按日拆分后上报；
- `reading_key` 保证重复上报幂等；维护期补录的迟到数据不会覆盖旧读数，只能让日报产生新版本（已签发版本内容永不改变，状态转为 `superseded`）；
- 负值（回退）和离群读数进入 `pending_review` 待核查队列，由运维人员确认（`confirmed`）或驳回（`rejected`），不会被悄悄丢弃；
- `/api/recycled-water/summary` 的每个汇总数字都带有构成它的 `reading_ids` 与逐来源明细，待核查和驳回读数列入 `excluded_readings`；
- 读数、核查决定和日报版本均保存在 SQLite，服务重启后保持一致。

```bash
.venv/bin/python tools/acceptance_recycled_water.py
```

该脚本会在临时数据库上启动真实 HTTP 服务，依次验收重复上报、跨日边界、核查流转、版本签发、迟到补录、最终汇总，并重启服务进程验证持久化。
