# 运价提取引擎 0.11.1

0.11.1 起，Trace 生产树固定为一群一行，不会因为每次拉取或历史批次而重复增加群记录。当前轮次和历史待检查轮次挂在同一个群下，历史问题通过“历史待检查批次”入口查看；群行单独显示最近一次成功拉取时间，尚未拉取和正在拉取使用明确文案，不用创建时间冒充。点选消息子轮次后群列表保持展开，并用独立选中态标出当前条目；群节点显示动态工作阶段，消息子项显示可读摘要、拉取时间、消息时间以及“已完成/已入库/当前阶段”状态。选中消息以详情任务状态覆盖可能滞后的群摘要，`WORKING / COMPLETED / INGESTED` 在左树、标题、链路节点和待审输出使用同一口径。顶部混合状态筛选改称“处理中”，实时队列计数排除已取消历史回填。右侧用“上次水位 → 本次拉取 → 当前水位”三级时间线替代技术 ID，完整展示拉取范围、消息时间范围和未推进原因。单群“立即解析”会持久保存所选群，手动触发合并同群未开始轮次后置顶队列，前端打开新任务并明确提示已入队等待 Worker。

“需检查”中的已结束轮次可直接“标记已处置”，填写说明并记录当前任务状态签名；处置不会把失败任务伪装成成功，只从待检查列表移除。后续任务状态、阶段或错误变化会使处置失效并重新出现在需检查列表。

0.10.0 起，抽取向导和独立盘点输出 `currency`，台账转换为 `币种`；原文未注明时默认 USD，EUR/CNY 等原币金额直接保留，不再用“见原图”占位。

0.9.21 起，Windows 下 DWS、水位脚本、登录探测和旧采集流水线的所有子进程统一使用 `CREATE_NO_WINDOW`；外部办公入口隧道改由 `wscript //B` + 零窗口 VBS 启动，定时重复触发也不再闪 PowerShell 控制台。

0.9.20 起，部分交付只把 `section_role=product` 的候选写入待审。提单清单、船期参考表、Local 费用和询价等 `reference/local_charge/inquiry` 分区只保留在来源证据和排除清单，不得转换成可检索、可放行的舱位或运价。

0.9.19 起，引用消息中的图片只下载为看板展示副本并计算 SHA-256，不进入 vision、extract 或独立盘点；只有本条消息自己的图片参与解析。引用图片下载失败不阻塞本条业务，引用正文仍可用于补充/修订上下文。

0.9.3 起，Agent 子进程失败会写入脱敏 `failure.json`；模型网关返回 404“model not supported”时按配置故障熔断 Worker。0.9.4 增加裸日期自动补月和未绑定周次降级备注。0.9.5 将无法安全自动修复的业务歧义自动转为 `NEEDS_REVIEW`。0.9.6 增加来源分区。0.9.7 在 Trace Studio 中提供就地处置。0.9.8 统一船名航次格式。0.9.9 将判噪回执纳入群级对账。0.9.10 增加业务失败的部分解析交付与 `PARTIAL_REVIEW_COMPLETE` 正常对账终态。0.9.11 将带价现舱统一为同源运价 + 舱位双入，并拆分两类专属字段。0.9.12 修复无效港名归一后部分候选身份碰撞，保留原始值并完成唯一待审读回。0.9.13 让技术失败待核携带已完成阶段、产品/盘点数量和事实摘要。0.9.14 在 extract 已成功而后续阶段技术失败时保留已有产品并形成带 technical 标记的可见部分待核。0.9.15 在 Trace Studio 展示全部消息子轮次并按多列排布，补齐中文部分解析状态，修正父轮次“需检查”统计，并支持从群链路直接下钻到消息条目链路再返回。

运行形态是单个本机常驻服务，内部包含工作日 09:00–13:59 每小时、14:00–18:59 每 30 分钟运行的 Asia/Shanghai 调度器，默认 3 个 Worker 执行线程和同一进程内的 Trace Studio HTTP 服务。周末不拉取，B 分片相对 A 延后 5 分钟。服务直接生成幂等触发并写入持久 SQLite 队列，不依赖 Windows 计划任务、`schtasks`、Codex 自动化时钟或外部守护器。调度配置每个 tick 重新读取，修改 `config/schedule.json` 即可热更新时段和启停，已入队任务不受影响。

```text
python orchestrator.py start-service --codex <CodexCLI> --dws <DWSCLI> --port 8797
python orchestrator.py service-status
python orchestrator.py stop-service
```

`start-service` 使用当前 Windows 用户的回环 HTTP、`worker.lock` 单实例锁和脱离终端的服务进程，启动后必须看到真实线程心跳才返回；`stop-service` 只写优雅停止请求并等待退出，不调用进程强杀。`worker-health.json` 记录上次调度、下一槽位、Worker 线程、队列计数和 DWS 状态，`service.log` 保存分级运行日志，`/api/health` 返回同一服务快照。DWS 认证失败只记录固定安全原因，不保存 CLI 原始诊断或凭据。原 `serve` 只用于只读审查；不能与 `service` 同时占用同一控制库和端口。

首次切换生产前必须停止旧 `serve`，确认 DWS 已由原账号重新授权；在调度窗口内启动会立即按当前槽位入队。legacy prompt/pipeline 保留为历史恢复入口，不能和新服务同时采集同群。

`/api/runtime/trigger` 的 JSON 可选 `message_id`。指定时只允许该群当前最旧未登记消息，已存在于控制库的水位边界会先过滤；不合规请求拒绝，避免 canary 水位越过缺口。

固定历史窗口使用 `enqueue-history`；它完成后只推进 `history-backfill.json`，不调用实时 watermark。

群阶段：pull → fanout → reconcile → watermark。每条消息独立持久子轮次：prepare → classify → vision（有图才执行）→ extract → inventory → coverage → audit → normalize → validate → submit → readback。父轮次等待子轮次时不占worker；同群后轮不能越过前轮。默认并发3，租约120秒，10秒续期；基础设施3次尝试，30秒间隔；业务最多一次来源重读。业务失败会把已识别产品写成带“部分解析待核”标记的待审行并逐条读回，再写精确 review issue；没有可用产品时仍写可见待核。回执确认后子轮次进入 `PARTIAL_REVIEW_COMPLETE`，下游完整解析阶段软跳过，父批次可正常对账并推进水位；该终态不证明解析完整，也不自动放行。

原文、引用原文、真实时间、发送人ID和消息ID保留。原图先取得唯一文件并复制到轮次快照，逐阶段核对SHA256；引用图片使用图片所属原消息ID下载。群知识包含全局文件、共享规范、本群规范和知识。独立盘点不看解析结果；机械覆盖先权威解析港口名称和机械转换日期，再逐产品对账。相同报价的运价/舱位字段并集只产生一行，冲突不合并。

台账写入仅预校验→POST /api/pending（保留其他来源与已存在来源行）。回执不明确时下次先查询实际条目，只补缺项；读回核对台账自身产品指纹和原文来源，不能用条数凑覆盖。预校验不通过的源事实可送入待审核，不能自动放行。读回完毕才锁内比较原水位并提交，任何并发变化都保留原交付回执而不越过。

Worker 默认模型 gpt-6-sol；gpt-5.6-luna 在真实报价测试多次产生空价、重复行和船期扩张，未用于正式接管。保留用户配置的提供方，不输出凭据、不使用 `--ignore-user-config`。整个服务用 OS 文件锁保证单实例；每个 Agent 阶段仍启动独立 Codex 子进程，模型或命令异常不会改写已持久化来源和队列。

当前 Codex CLI/提供方可能不支持 `--output-schema`。Agent 不再依赖该参数，而是把同一 JSON Schema 嵌入只读 Prompt，严格解析最终 JSON，再由本地 `validate_schema` 做字段和类型校验。认证失败只从 stderr 的明确 401/403 标记识别，不能搜索长 Prompt，以免业务文本中的“401/403”造成误报。

监控入口：[Agent工作监控台](http://127.0.0.1:8796/) → 采集编排。同源只读展示父子轮次、阶段、失败、回执和水位；默认隐藏开发验收轮次，可勾选显示。显示“处理完成”代表已送审对账，是否放行以台账人工审核为准。是否接管及worker存活均从实际标记/心跳读取。

审查工作台：[Trace Studio](http://127.0.0.1:8797/studio)。页面以可拖拽、滚轮缩放的无限画布为主体，生产树只作为入口；轮次标题、进度、恢复操作和高密度监测明细集中在画布右侧浮动工作坞，不占用画布下方空间。悬停节点时在节点旁显示单一综合列表，集中呈现状态、耗时、尝试次数、阶段指标、摘要和异常；列表鼠标穿透，移开即收起，不会阻挡节点点击。中央 Trace Map 使用节点和动态曲线表达证据、AI 阶段、机械门禁与待审输出，群级轮次会展开消息分支；右侧用紧凑键值行、阶段指标、异常块和事件摘要展示详情，来源、证据、输出和事件通过标签切换。SSE 先发 `sync_ready` 序号，再以全局 event seq 通知变化；浏览器使用 `Last-Event-ID` 自动续传，整页所有组件从同一轮次快照原子刷新。恢复操作只允许既有 `resume`、`recheck_inventory`、`recheck_source`，经 `/api/trace/action` 提交。

工作坞“运行”标签提供统一控制面：DWS 登录状态和一键浏览器授权、内部调度窗口及下一 A/B/C/D 槽位、最近持久触发、模型与 Worker 容量、线程状态、当前任务和队列状态。`GET /api/runtime` 每 5 秒刷新；`POST /api/dws/login` 只启动 `dws auth login --recommend`，登录态后台轮询，不接收或回显凭据。点击触发器或正在执行的任务可直接定位到对应 Trace 轮次。只读 `serve` 也支持 DWS 授权和调度预览，但不会启动调度器或 Worker。

“运行”标签可以直接编辑并保存内部调度和工作进程：启停调度、Tick、工作日、A/B/C/D 偏移、时间窗，以及 Worker 领取开关、模型、并发、Codex/DWS CLI。调度、Worker 领取、模型和 Codex CLI 在常驻 `service` 中即时生效；新模型从下一个 Agent 阶段开始使用，当前已启动的模型子进程不会被中断。并发数仍需重启服务后生效。`config/schedule.json` 和 `config/runtime.json` 是持久化真源，`POST /api/runtime/schedule`、`POST /api/runtime/worker` 只做校验和原子保存，不接受任意命令。完整 15 阶段执行链会同时展示阶段、执行者类型和代码模块；Agent 阶段仅指临时 Codex 子进程，机械门禁和台账 IO 不调用模型。

“单群立即解析”允许从已登记群聊中选择一个群并创建唯一手动触发；`POST /api/runtime/trigger` 复用同群未开始的自动轮次并将该轮次置顶，没有可复用轮次时才创建新父轮次，不修改自动调度开关。Worker 暂停时拒绝触发；Worker 启用时仍按既有并发、同群保序、依赖和待审边界执行。

维护命令（--runtime位于子命令前）：

```text
python orchestrator.py enqueue-production --shard A --trigger-key unique-trigger
python orchestrator.py enqueue-production --shard A --only <slug> --message-id <oldest-message-id> --trigger-key unique-canary
python orchestrator.py enqueue-history --only <slug> --history-start "2026-10-19 00:00:00" --history-end "2026-10-22 17:36:00" --batch-size 3 --trigger-key unique-history
python orchestrator.py service --codex <既有CodexCLI> --dws <既有DWSCLI>
python orchestrator.py start-service --codex <既有CodexCLI> --dws <既有DWSCLI>
python orchestrator.py service-status
python orchestrator.py stop-service
python orchestrator.py resume --run-id <失败轮次>
python orchestrator.py resume --run-id <覆盖失败轮次> --recheck-inventory
python -m unittest test_schedule test_service_control test_runtime_control test_production test_orchestrator -v
```

resume只重启失败阶段，不抹掉成功产物。recheck-inventory仅用于已定位和修复执行规则后的维护恢复，不自动追加业务重读预算。人工编辑/已审核产品存在差异时读回阻止自动覆盖，需维护处理。

2026-10-12恢复补充：`resume --run-id <轮次> --reread-source`仅用于代码/契约已修复且未送审的coverage/audit/normalize失败轮次；重新执行extract及后续阶段，保留prepare/原图/源消息、历史阶段产物和已消耗的业务重读预算。已送审或正在运行的轮次拒绝重读。此维护入口不由自动触发调用。

名称解析按原词去重并以台账接口200项上限分块；两端日期先执行同一转换，有效期允许明示单周/区间，ETD只允许单日。原文明示不同箱型箱量与箱数的舱位分配保留独立条目，真正重复或冲突继续阻塞。TEU/FEU不直接换算实际箱数；ETA和还空箱条款保留备注。DWS登录态失效显示固定的重新授权原因，不落原始认证诊断。

不同提单/物理行使用 `_source_row`（解析）与 `source_row`（独立盘点）对账：优先 `bill:<原始提单号>`，否则 `image:<图序>:row:<含表头物理行号>` 或 `text:<原文行号>`。进入台账时仅通过已有备注字段保留 `来源行=`，不新增业务接口字段；同一来源行重复仍报错。截断港名不补尾，完整国家后缀两端原样保留；所有主数据映射仍走统一接口，不凭前缀猜港。

原图下载归属取DWS资源的 `download.arguments.message-id`；历史排队源在prepare阶段从父轮次原始pull回执重建归属，验证原文、时间和资源集合一致，只改图片owner并保存恢复回执。回复业务消息ID不改变。失败通知网络异常记录日志，但不能杀死执行线程、留下“只有健康心跳”的假在线。

8796监控汇总完整真实生产队列，与最近100条列表分开；历史验收轮次不计入生产阻塞。worker心跳在线、业务执行、阻塞、等待与空闲分别展示。

边界：永久失败会阻塞该群水位，但常驻服务和其他群继续工作；恢复需先修正具体原因，再 resume。尚未引入无依据的强制 DONE/自动批准。

成功POST待核报告保留本地review-issue.json；完整来源读回后允许凭该回执结案，严格核对原文、pending状态、完整ID集合和产品指纹，结案失败重试。历史无回执保持open，不访问受限GET、不猜覆盖。日期同时接受原文明示中文年月日/月日，不接受可能等修饰。
