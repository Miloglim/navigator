# 人工维护台账（局域网版）

**独立产品**：只依赖 Python 标准库，自带 SQLite 库 `freight_manual.db`，
不引用、不读写任何其他看板的代码或数据。

功能：航管端录入（港口/船司/航线主数据归一、柜型三档、价格类必须整数、录入时间自动盖）、
批量导入（固定列序横排周表 + 港群）、价舱管理就地编辑与**留痕更新**、
主数据后台维护（港口 / 船司 / 航线 / 发送人 / **港群**）、销售端折叠查询与 CSV 导出。

跑在自己电脑上，不经过任何托管平台，**没有平台水印**，断网可用。

## 港群（用户自维护的港口群）

库里存群名（如 `NEB`），**查询时双向展开**：筛成员港 `Pecém` 会同时给出 `NEB` 的群价；
筛 `NEB` 会连成员港的精确价一起给；非成员港不串。录入时目的港填 `NEB` 或 `北巴` 都认。

已按业务口径预置 6 个群（WCSA / ECSA / NEB / WCCA / CAR / MEX）。改成员、加新群在
「航管端 → 账号 & 群聊 → 主数据库 → 港群」里做；重灌种子用 `python seed_groups_local.py`
（`--check` 只看成员解析结果，归一不上的会列出来不猜）。

## 起

```
start-manual.bat                                          后台起服务并打开 http://127.0.0.1:8795/
stop-manual.bat                                           停（按命令行特征清掉所有 manual_server 进程）
powershell -ExecutionPolicy Bypass -File allow-lan.ps1    放行同事访问并打印内网地址
```

前台调试：`python manual_server.py --host 127.0.0.1 --port 8795`

**为什么是 8795 不是 8790**：`127.0.0.1:8790` 上留着别的实例，而 Windows 允许 `0.0.0.0`
与 `127.0.0.1` 同时绑同一端口，浏览器访问 `127.0.0.1` 时走更具体的那条，会打开成另一个看板。
现在启动前会先探端口，有人应答就拒绝启动并提示换 `--port`，重复双击也不会起第二份。

## 数据从哪来

- 一次性迁移：`python pull_from_cloud.py`（读已发布版的公开接口，写进本地库；主数据表按快照对齐并清掉改名后的旧行）。之后本地库就是唯一事实源，**不再依赖云端**。云端地址放本目录 `cloud_url.txt`。
- 备份/回滚：`GET /api/export` 出整库快照 JSON；`python manual_server.py --import 快照.json` 灌回来。

## 自检

```
python manual_check.py           接口全量回归（跑在库的副本上，不碰真库）
python validity_rule_check.py    有效期区间解析的表驱动用例
```

## 批量导入（航管端「批量导入」页签）

固定列序的横排周表，一行的 `20GP / 40HQ/HC / 40NOR` 三个价格列拆成对应柜型的多条记录：

```
船司 · 起运港 · 目的港 · 20GP · 40HQ/HC · 40NOR · 有效期 · Freetime · 备注
· 航线 · 船期ETD · T/T航程 · 亏舱费 · 来源群 · 发送人 · 消息原文 · 序号 · 更新目标
```

- 从 Excel/WPS 框选复制直接粘进来，或选 CSV/TXT；带表头按**列名**对位（换了列序不怕），没表头按固定列序
- 只有 `船司 / 起运港 / 目的港 / 价格` 必须逐行填，其余留空＝沿用上一行
- `有效期` 一格写区间即可（`9.10-9.16`、`9月10日-9月16日`、`2026-09-10 → 2026-09-16`、
  `20260910-20260916` 都认，跨年自动给截止加一年），入库仍是 `valid_from` / `valid_to` 两个 ISO 日期
- 流程：预检（含归一前后对照与逐行报错，可导出错误行 CSV 改完再贴）→ 确认入库 → 回执
- 两种模式：新增；**留痕更新**（按 船司+起运港+目的港+ETD(+柜型) 定位当前生效行，匹配 0 条或多条一律报错不猜）
- 目的港可以写港群名（`NEB` / `北巴`），按群价存一条不拆成员
- `.xlsx` 先转 CSV：`python batch_import.py --file 周表.xlsx --out 待导入.csv`

## 文件

| 文件 | 作用 |
|---|---|
| `manualdb.py` | 数据层：建表、主数据归一、指纹幂等写入、留痕、查询与折叠、频次、导出导入 |
| `manual_server.py` | HTTP 服务，接口与已发布版一致，多 `/api/export`、`/api/import` |
| `web/index.html` | 前端页面（本机回环免口令） |
| `pull_from_cloud.py` | 一次性迁移入口 |
| `seed_groups_local.py` | 港群种子 |
| `batch_import.py` | `.xlsx → CSV` |
| `manual_check.py` / `validity_rule_check.py` | 回归与规则用例 |
| `start-manual.bat` / `stop-manual.bat` / `allow-lan.ps1` | 起停与放行 |
| `token_manual.txt` | 写入令牌（首次启动自动生成；本机浏览器免口令） |

## 说明

云端版（`pwle9fvs.qwenwork.host`）冻结在最后发布版本，不再迭代；今后所有更新与优化只改本目录。
