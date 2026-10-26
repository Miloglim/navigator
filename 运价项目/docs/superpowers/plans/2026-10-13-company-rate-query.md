# 公司系统运价查询接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 销售查询保持本地运价即时返回，并在用户勾选后独立查询、展示和保存公司内询运价或船公司实时价格。

**Architecture:** 新增纯契约与请求规范化层、IO 编排层、薄路由层和独立持久表。现有 `/api/rates` 不调用外部网络；页面并行调用新任务接口并轮询结果。外部 HTTP、历史保存和失败均与本地查询隔离。

**Tech Stack:** Python 标准库、现有 SQLite/PostgreSQL 适配、现有单页 HTML/JavaScript、`unittest` 与 Node DOM 测试。

**Spec:** `docs/superpowers/specs/2026-10-13-company-rate-query-design.md`

## Global Constraints

- 不新增依赖；HTTP 使用 Python 标准库。
- 新 API 统一 `{ok,data?,error?,code?}`。
- 令牌只从 `YQN_DINGTALK_TOKEN` 读取，用户 ID 只从 `YQN_USER_ID` 读取，均不得返回前端或写普通日志。
- 本地 `/api/rates` 和 `/api/rates_grouped` 不等待外部 HTTP 或外部结果写入。
- 最多 20 个港口组合；公司内询有效复用 15 分钟，船公司实时价格 5 分钟，均以 `_seconds` 配置键命名。
- 本期不接入船期、舱位和附加费。

---

### Task 1: 契约与纯请求构造

**Files:**
- Create: `Database/core/company_rates_contracts.py`
- Create: `Database/services/company_rates.py`
- Test: `Database/test_company_rates_service.py`

**Interfaces:**
- Produces: 任务路由常量、上限/有效期常量、查询规范化、港口组合、内询/实时网关请求体构造和响应规范化。

- [ ] 写失败测试，覆盖最多 20 组、单值港口请求、实时任务 ID 仅取当前创建结果、错误响应拒绝。
- [ ] 运行测试确认因模块缺失而失败。
- [ ] 实现最小纯函数并运行测试通过。

### Task 2: 独立持久化与账号偏好

**Files:**
- Modify: `Database/manualdb.py`
- Modify: `Database/postgresdb.py`
- Modify: `Database/core/contracts.py`
- Modify: `Database/services/account_forms.py`
- Test: `Database/test_company_rates_store.py`
- Test: `Database/test_account_profile.py`

**Interfaces:**
- Produces: `company_rate_tasks`、`company_rate_results` 表及任务/结果读写；两个布尔账号偏好。

- [ ] 写失败测试，覆盖历史长期保留、有效期复用、过期不算当前、偏好跨会话保存。
- [ ] 运行测试确认预期失败。
- [ ] 实现最小表结构、迁移和偏好校验，再运行测试通过。

### Task 3: 网关客户端与异步任务编排

**Files:**
- Create: `Database/ops/company_rates.py`
- Test: `Database/test_company_rates_runtime.py`

**Interfaces:**
- Consumes: Task 1 请求/响应函数，Task 2 存储函数。
- Produces: `create_company_rate_task`、`create_realtime_rate_task`、`get_task`。

- [ ] 用本地假 HTTP 服务写失败测试，覆盖 Authorization、xUserId、组合独立失败、超时和异步保存。
- [ ] 运行测试确认预期失败。
- [ ] 使用 `urllib.request` 和受限线程池实现，运行测试通过。

### Task 4: HTTP 路由、鉴权与服务接线

**Files:**
- Create: `Database/transport/company_rate_routes.py`
- Modify: `Database/transport/access_rules.py`
- Modify: `Database/manual_server.py`
- Test: `Database/test_company_rate_routes.py`

**Interfaces:**
- Produces: `POST /api/company-rates/query`、`GET /api/company-rates/tasks?id=...`、`POST /api/company-rates/realtime`。

- [ ] 写失败 HTTP 测试，覆盖销售登录、未授权、参数错误、任务创建和状态读取。
- [ ] 运行测试确认预期失败。
- [ ] 接线薄路由与统一 envelope，运行测试通过。

### Task 5: 销售页面交互与分来源展示

**Files:**
- Modify: `Database/web/index.html`
- Create: `Database/test_company_rates_ui.cjs`

**Interfaces:**
- Consumes: Task 4 API 和账号偏好。
- Produces: 两个记忆勾选项、公司内询异步区、实时价格显式按钮与状态展示。

- [ ] 写失败 DOM 测试，覆盖默认偏好、仅勾选时调用、实时显式触发、本地结果不等待。
- [ ] 运行测试确认预期失败。
- [ ] 实现紧凑控件和异步渲染，运行 DOM 测试通过。
- [ ] 启动本地服务，用桌面和手机视口做浏览器操作与截图核查。

### Task 6: 契约文档、版本与总体验证

**Files:**
- Modify: `Database/docs/IPC契约.md`
- Modify: `Database/docs/架构基底.md`
- Modify: `Database/docs/版本记录.md`
- Modify: `D:/Obisian DB/Database/项目/运价台账/当前状态.md`
- Modify: `D:/Obisian DB/Database/项目/运价台账/审阅记录.md`

- [ ] 更新新增模块、配置键、API、数据表和版本记录。
- [ ] 运行所有新增测试、账号偏好回归、现有销售查询测试和语法检查。
- [ ] 运行知识库 `scripts/check_vault.py`，核对并记录项目同步状态。
- [ ] 检查工作区差异，确认未覆盖无关用户改动。
