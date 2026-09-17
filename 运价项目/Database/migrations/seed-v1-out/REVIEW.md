# Seed alias_scope 草稿 REVIEW（v2，含数据反查验证）

**时间**：2026-09-15 21:22
**脚本**：
- `Database/migrations/seed_alias_scope_v1.py`（分类，dry-run 不写库）
- `Database/migrations/verify_seed_v1.py`（2787 条真实语料反查证据强度）
- `Database/migrations/audit_dirty_values.py`（业务表脏值审计）
- `Database/migrations/audit_ctype_combo.py`（BUG 2 深挖）

**基线快照**：`freight_manual.db.pre-p1-baseline-20260915-195916`（文件 4.4MB）+ `pre-p1-export-20260915-195940.json`（API 4.2MB）

---

## 1. Seed 分类结果（v2 稳定）

| 分类 | 条数 | 是否入库 | 说明 |
|---|---:|---|---|
| **true_alias** | **27** | ✅ 现 DDL 4 kind 全支持 | 中文段名→代号、缩写→规范名 |
| **enum_norm** | **9** | ⚠️ 要 DDL 扩 kind 到 8 值 | space_type 8 + ctype 1 |
| **self_map** | **11** | ❌ 剔除 | alias==target 且 target 是主数据规范名，冗余 |
| **disposal** | **12** | ❌ 另立表 | "拆两条/进备注/剥掉" 等指令，不是别名映射 |
| **inherit** | **1** | 处理 | ONE 群引用"同华南-拉美"，seed 需展开 |
| unclassified | **0** | — | 全部自动分类，无待人工裁定 |
| 合计 | 60 | | |

**self_map 11 条**：RIO HAINA、ECSA(×2)、WCSA(×2)、IZMIT、MERSIN、GDANSK、Hamburg、Rotterdam、地东 —— 全都在主数据里就是规范名，`build_index` 已能直查，无需 alias_scope 冗余。

---

## 2. 数据反查验证结果（2787 条真实语料）

用业务表 + pending.payload 里所有 message_text 反查每条候选的证据强度，验证脚本 v4：
- **主判定**：消息含 alias 时，本群 kind 字段的入库值分布众数是不是 seed 给的 target
- **辅助信号**：alias 附近 40 字内是否出现 target 变体（"窗口印证率"，非硬门槛）
- **hit_target 判定**：用 variants 折叠集匹配（解决中文 target vs 英文 pol 值 fold 不等）

**verdict 分布（36 条已入候选全部验证）**：

| 档 | 数 | 备注 |
|---|---:|---|
| 强支持（窗口印证 100%） | 2 | 欧洲 POD欧基/欧基 → 欧基港，13/13 完美 |
| 强支持 | 1 | 欧洲 全口岸 → 全国基本港，13/13 |
| 支持 | 1 | 土耳其 伊斯坦布尔 → Ambarli |
| 支持（众数非唯一） | 3 | ISTANBUL/NYC/ORF，多港报价错伤 |
| ⚠️ 反例 | 17 | **窗口=0% 的真反例 0 条**（全 17 条都是"多港错伤"或"pending 中间态未归一"，seed 判定实际正确） |
| 无样本 | 12 | 本群语料未覆盖 alias；memory 硬数据支持（NB 98 次全宁波等） |
| 跳过 | 0 | 修了 bug 后无字段映射缺失 |

**结论**：**验证脚本没揪出任何"seed 判错"的候选**。27 条 true_alias + 9 条 enum_norm 可入。

---

## 3. 三个真发现

### 发现 A｜**自映射 11 条要剔除**
之前 seed 里 `ECSA→ECSA`、`WCSA→WCSA`、`Hamburg→Hamburg` 这类"写法就是主数据规范名"其实**不需要入 alias_scope**，主数据 `ports.name` / `port_groups.name` 直接匹配即可。v2 已分流到 `self_map.csv`。

### 发现 B｜**BUG 1（美加 pod 未归一）证伪** ✋
上一轮我看到验证分布里 "NYC×4; SAV×3; HOU×3" 判断业务表存了未归一代号。**审计脏值脚本扫过：业务表 freight_rates/space_records 的 pol/pod/carrier/route 脏值 0，全部命中主数据**。

之前那个分布来自 `pending.payload["目的港"]` 字段——是 AI/record_builder 生成 pending 时的**中间态**（未审），审后 expand_cn 会归一。**pending 中间态未归一 ≠ 业务表脏**。

### 发现 C｜**BUG 2（柜型 40HQ/HC 复合值）铁证坐实** ⚠️⚠️⚠️

**独立于 seed 的生产代码 bug，严重度高于本次所有 seed 发现**：

| 位置 | 数字 | 说明 |
|---|---:|---|
| `freight_rates.container_type='40HQ/HC'` | **135** | 占 rates 总量 279 的 **48.4%** |
| `space_records.container_type='40HQ/HC'` | 4 | |
| `pending_records` 里 柜型='40HQ/HC' awaiting | **821** | 未审的都带同样问题 |
| `pending_records` 里 柜型='40HQ/HC' approved | 139 | |
| 135 条 rates 的 origin 分布 | **100% agent** | 全走 AI/record_builder 路径 |

**违反的铁律**（memory 记录）：
> "柜型三档白名单且取消 norm_ctype 把 40GP 悄悄归成 40HQ/HC"

**观察证据**（抽样 rates）：消息原文写 "**20GP、40GP**" → AI 归到 "40HQ/HC" 复合值入库。

**根因猜测**（未确认，需要专项排查）：
- `expand_cn` 里 `ctype_list` 贪婪分词识别 "40HQ/HC" 作为**单 token**（memory 有记载"40HQ/HC 自带斜杠不能裸切"），可能因此**不拆**、原样入库
- 或 `validate_record` 三档白名单没实际生效（`ctype_ok` 判定顺序问题）
- 或 record_builder 生成 pending 时直接把 AI 抽出的 "40HQ/HC" 传给"柜型"字段，审时闸门未拦

**这是当前项目独立于 seed 的**、影响 135 条已入库数据的治理问题。修它比 seed 灌库优先级更高。

---

## 4. 现在的动作建议

### 立即动作（等你批准）
1. **seed 侧**：27 条 true_alias + 9 条 enum_norm（DDL 迁移后）灌入 alias_scope 表。self_map/disposal 不入库。inherit ONE 群脚本里手工展开
2. **BUG 2 独立排期**：柜型 40HQ/HC 是**当前项目最高优先级 bug**，135 条已入库 rates 都受影响。建议开专项：查 expand_cn / ctype_list / validate_record / record_builder 四处理路径，定位归一失败点，做数据修复（135 条已入库 rates 需不需要拆成两条？还是改成 40HQ 一档？）

### 三件等你拍板的事

**Q1｜BUG 2 处理策略**
- **A. 先修 bug 再灌 seed**：修 expand_cn/ctype_list 后重跑历史数据回填 135 条，再灌 seed。最稳，但 delay 2-3 天。
- **B. 并行**：seed 灌库（发现 3 pending 中间态归一有 alias_scope 会更早生效），BUG 2 独立开分支修。
- **C. 只灌 seed，BUG 2 后续处理**：本次不动 135 条 rates。

推荐 **B**（seed 与 ctype bug 独立，先动 seed 反而能让 alias 归一在 pending 阶段就生效，减轻审核员负担）。

**Q2｜DDL 迁移的时机**
现 DDL kind CHECK 只允许 4 值。enum_norm 9 条要 `space_type`/`ctype`，disposal 里也有 price/remark 等（另立表的话不需要）。
- **A. P1a 前一次性扩 kind 到 8 值 + 加 alias_kind/scope_column/evidence_count**：DDL 迁移一次做完，灌 27+9=36 条
- **B. P1a 只灌 4 kind 支持的 27 条，P1b 再 DDL 迁移灌 9 条**：分两步，中间 alias_scope 处于"半扩"状态

推荐 **A**——反正 alias_scope 生产写 API 也要一并做（P1c），DDL 一次迁移到目标态、少一次改动。

**Q3｜宁波群"南美西墨西哥"→ WCSA 只入 1 条**
memory 铁律：`南美西墨西哥` → WCSA **和** MEX 两条都要存。seed 脚本目前只提取 WCSA（targets 截取前 1 条）。
- **A. 手工补 MEX 一行到 true_alias.csv**
- **B. 改 seed 脚本：kind=pod 且 targets 数量>aliases 时，如果多出来的 target 也能命中 port_group 主数据，不截取、展开为多条**

推荐 **B**——写进脚本更持久，未来规范里出现类似"一片两港群"能自动吸收。

---

## 5. 完整清单文件

`Database/migrations/seed-v1-out/`：
- `true_alias.csv` 27 条 — 待入库
- `enum_norm.csv` 9 条 — DDL 迁移后入库
- `self_map.csv` 11 条 — 冗余不入库
- `disposal.csv` 12 条 — 另立表
- `inherit.csv` 1 条 — 引用继承
- `summary.json` — 分类统计
- `verification.csv` — 每条候选的验证结果
- `verification.md` — 验证报告分组清单

---

**你回复三件事**：
1. Q1 选 A / B / C
2. Q2 选 A / B
3. Q3 选 A / B

**批准后立即动 P1a 的 --write 分支**。
