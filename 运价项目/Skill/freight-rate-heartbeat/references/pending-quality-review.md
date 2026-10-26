# 待审核主动业务复核（5.12.0）

用户于2026-09-29授权省去人工贴消息ID：Agent自动领取待审消息组，结合原文、原图和对应群知识审查，有依据时修正一次，保留待审并沉淀依据。它是独立后台任务，不修改采集状态机、水位、定时采集prompt或机械校验结论，不自动放行。

## 固定入口与领取契约

项目根 `C:\Users\YQN\Documents\正式版运价库\运价项目`；脚本 `Skill\freight-rate-heartbeat\scripts\pending_quality.py`；Python使用已安装的 `C:\Users\YQN\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`。以下相对命令须在脚本目录执行，自动任务应传绝对脚本路径。

1. 每轮 `pending_quality.py next --limit 2`。返回统一 `{ok,data?,error?}`；data.items为空静默结束。只领取自动采集、awaiting的rates/space，以来源群+消息ID合并两表完整组。高风险的已过组优先，没有风险信号的组也排队，不能只看failed。
2. 逐组读取evidence_file的source.rows、逐字source_text以及knowledge_files中的全部契约、全局规则、本群规范、共享规范、业务沉淀；逐张打开original_images核对完整表格/页脚/定义，不以OCR缓存代替原图。引用内容与本次附件必须分开。记录实际读过的路径及其作用；哈希证明文件版本，不证明理解正确。
3. 核查每个主报价块的起运港、目的港/港群及跨国同名、船司、柜型与配价、所有港口/船期行、运价/现舱分块、条件费用、船名航次、有效期与ETD、主图/引用图。校验passed只代表字段合法，不代表业务正确。风险信号只排序，不能据此直接修正或认定错误。
4. 缺原图时按DWS技能读取本条确切消息/附件，保留来源证据，下载到Database/images；原文无法核实、群路由不存在、规范冲突则needs_human。补证据后且尚未修改业务数据前，运行 `pending_quality.py refresh --claim <claim>`，它会核对待审版本未变并重新生成证据包。缺证据不能谎报reviewed/repaired。

## 有依据修正的边界

- 领取有效期1800秒。开始写前再次refresh，并读取本组当前待审；核对claim未过期、所有条目仍awaiting、payload签名一致、无新edit_log。领取避免两个复核任务重复处理；修复端expected_signature保护逐条并发修改。不能把领取当作锁住人类审核。
- automatic_repair_allowed必须为true。最终机械failed或已有任何编辑日志的组仅复核/转人工，不能重开修正循环或覆盖人工。自动采集首次JSON预检→Agent一轮修正→pending最终校验流程维持原样。
- 主数据只认GET /api/master?full=1。判断是错字、别名缺口、真实缺港还是歧义；必要时查官方资料。有官方证据的新港/有群依据的别名仅走ledger_client.ensure_master，不覆盖主数据、不制造全球别名。无依据保持未知，转人工。
- 保存修改前整组快照、拟改字段及依据到Database/ops/reports/pending-quality-YYYYMMDD/。只修当前源已过且未审的Agent条目，走ledger_client.repair_pending，逐条带expected_signature和真实group_spec/original_image/official_source依据。它保留编号、原始payload和编辑日志；禁止直接SQL写业务库、正式记录接口、放行/驳回/删除接口。
- 漏行若原文/原图明确，只把确认缺失的产品交给 `ledger_client.submit_missing()`；它向POST /api/pending同时传两个保护开关，保留同源已有编号和编辑日志，只新增缺行。禁止用普通 `submit()` 重提修复后的整组，禁止覆盖其他来源；未经独立原图核对不得宣称补齐。不能通过改通过/未过结论实现整组展示。
- 业务只修一轮。写入后的服务端结果和重新读取用于确认，不触发第二轮修正，也不调用采集advance推进水位。剩余错误/不确定/并发改变交人工。传输异常先读现状，不盲重试已成功的修复；部分成功留快照和逐条回执，不再自动重写已修条目。

## 结案、监控与沉淀

报告JSON保存项目内：`{"outcome":"reviewed|repaired|needs_human","note":"具体结论和未解决问题","evidence":["实际读取的绝对文件路径"]}`。reviewed表示全组核对无须修改且版本未变；repaired表示保留原编号/来源，确有修改，最终全部passed/awaiting；needs_human表示依据不足、已有编辑受保护、最终未过或处理受阻，并写具体字段、冲突证据和处理位置。

运行 `pending_quality.py finish --claim <claim> --report <报告绝对路径>`：检查全部知识/原图证据、版本、来源身份、未审状态及修后结果；修后新指纹同时结案，避免修复触发自身重跑。已审核/并发改动导致finish失败时保留证据，不强行覆盖；下轮领取刷新版本。审查中断最多重新领取一次，连续两次过期转人工。status计数按审查版本，修前/修后均保留，不能将版本数当成消息数。

next/finish会尝试唤醒8796监控端并报告BUSINESS_REVIEW和当前群/消息；Agent在“读原图/查主数据/修字段/结果核对”阶段调用现有agent_watch.transport.client.report报告具体活动，禁止只用开始/结束掩盖长阶段。每组检查开始、任何业务写入前调用现有agent_watch.transport.client.checkpoint，遵守监控端的暂停/继续；暂停不授予任何额外业务写权限。监控失败记录警告不修改业务数据。

先finish确认结果，再写本群业务知识或已有规范中窄范围、可复用、有来源的条款；若是确定的程序陷阱，修代码并补必要回归，更新版本/CLAUDE。不要给每组复制大段日志、不要把一次推断升格为全局事实，也不要修改Codex记忆目录。新知识路径要经spec_index路由验证；本次明确POL优先和ETD推导已存在全局规则，只补本群案例，不造第二套规则。程序优化须可验证、有针对性，不能让定时Agent无依据地改公共API或部署。

无待处理项、核对正常且无新变化时静默；只通知有实际修复的简短汇总、需要用户决策的新增问题、持续故障或恢复，不在每轮发送健康日报。同一needs_human版本不重复领取或重复通知。保存文件和哈希无法保证模型100%正确；真实原图核对、审计、并发保护和人工放行分别提供证据与边界。
