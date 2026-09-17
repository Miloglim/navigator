# 群规范三层重构 + 演化闭环方案 v1

> 状态：设计草案，未动代码。用户 review 通过后按 P0-P4 分期实施。
> 生成日期：2026-09-14
> 前置侦察：Skill/freight-rate-heartbeat/references/群聊规范/*/规范.md（11 份）+ Database/manualdb.py + docs/data-model-spec.md + SKILL.md

---

## 0. 现状事实清单（不改这里就没法谈方案）

| # | 事实 | 出处 | 影响 |
|---|---|---|---|
| F1 | `alias_scope` 表 DDL 已建但代码零引用 | manualdb.py:153；MASTER_TABLES 不含 | 规范文档说要用、实际是死表 |
| F2 | alias_scope.kind 只有 4 值（pol/pod/carrier/route） | DDL CHECK 约束 | 装不下缩写表实际的 8+ 维度（舱位/柜型/价格/备注/船期） |
| F3 | 缩写表混装三种异质映射：真别名 / 枚举归一 / 处置指令 | 11 份规范抽样 | 二元 alias→target 结构表达不了后两类 |
| F4 | SKILL.md 已是"按需读整份"，非每条消息读全文 | SKILL.md:52-53 | Token 消耗比想象低，但仍可按分层再降 |
| F5 | `update_pending` 覆盖 payload，无 diff/audit 列 | manualdb.py:1088-1120 | 审核动作反向沉淀规则的数据源从根上缺失 |
| F6 | 全局规则（附加费/港群/default_pol/留空/逐字保留/ocr_cache）在 11 份里重复 | 群规范抽样 | 与 extraction-rules.md 职责重叠，改一处需改多处 |
| F7 | `expand_cn` 与 `resolve_core.port_side` 对未命中港处理不一致（软归一 vs 硬闸） | manualdb.py:483 vs 668 | 同输入两路径结果不同 |
| F8 | ONE 群规范近乎空壳，全部 delegate 华南-拉美 | ONE+ZIM+WHL+HPL+CSP 交流群/规范.md | "11 群各一份"名不副实，暗示群规范应支持继承 |
| F9 | `ensure_carrier` 只在 pending 入口触发，脏值会写进 carriers | manualdb.py:578；主数据-auto长尾收口候选-792项.md | 积压已发生，需要更严的形态闸或候选放行制 |
| F10 | Master 缓存非线程安全，`self.unmatched` 是实例级 list | manualdb.py:564, 844 | 现有隐患，不在本方案 scope，标记 TODO |

---

## 1. 规范职责拆分：五类混装 → 三层各司其职

### 1.1 现状解剖

11 份 markdown 每份都装五种性质完全不同的信息：

| 类别 | 特征 | 谁消费 | 现状载体 |
|---|---|---|---|
| ① 识别名→规范名映射 | 键值对，作用域敏感（列+群） | 代码 | markdown 缩写表 |
| ② 抽取模板 | 正则或 pattern | 代码 | markdown 报价格式节 |
| ③ 枚举白名单 | 值域约束 | 代码 | **代码已有**（SPACE_TYPE_ENUM 等），markdown 重复 |
| ④ 业务规则 | if/then 跨字段推理 | 代码+AI 兜底 | markdown 已知坑 + 解析要求 |
| ⑤ 软语境 | 发送人画像、群风格、事故叙事 | 仅 AI | markdown 头部 + 已知坑 |

### 1.2 分层方案

**L1 数据字典**（承载 ①）
- 载体：主数据表 `alias_scope`（复活 F1，扩 kind 破 F2，加 alias_kind 破 F3）
- 消费：代码直读，AI 不参与
- 位置：`Database/freight_manual.db`

**L2 规则库**（承载 ②④）
- 载体：YAML 配置
- 消费：代码执行，AI 兜底路径可读
- 位置：`Database/rules/common.yaml` + `Database/rules/groups/<群名>.yaml`

**L3 语境卡**（承载 ⑤）
- 载体：markdown，每份 <500 字
- 消费：仅 AI 兜底路径注入
- 位置：`Skill/freight-rate-heartbeat/references/群聊规范/<群名>/语境.md`（从"规范.md"改名）

**③ 枚举从规范文件里删掉**，代码里的 `SPACE_TYPE_ENUM / ctype_list / REQUIRED_BY_KIND` 是唯一真源。

---

## 2. L1 数据字典：alias_scope 复活与扩展

### 2.1 新 schema

```sql
CREATE TABLE alias_scope (
  alias        TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN (
                 'pol','pod','carrier','route',           -- 原 4 值
                 'space_type','ctype','price','remark'    -- 扩 4 值
               )),
  alias_kind   TEXT NOT NULL DEFAULT 'true_alias' CHECK (alias_kind IN (
                 'true_alias',    -- NB → Ningbo
                 'enum_norm',     -- 现仓 → 现舱
                 'disposal'       -- 大货单询 → 留空；+ENS → 进备注
               )),
  target       TEXT NOT NULL,     -- 对 disposal 存 JSON 指令
  scope_group  TEXT NOT NULL DEFAULT '*',  -- '*' = 全局
  scope_column TEXT NOT NULL DEFAULT '*',  -- '*' = 全列（可选精细化）
  evidence_count INTEGER DEFAULT 0,
  source       TEXT DEFAULT 'manual',      -- manual | candidate_approved
  note         TEXT,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (alias, kind, scope_group, scope_column)
);
```

### 2.2 匹配序（最窄 → 最宽）

1. `(alias, kind, scope_group=本群, scope_column=本列)`
2. `(alias, kind, scope_group=本群, scope_column='*')`
3. `(alias, kind, scope_group='*', scope_column=本列)`
4. `(alias, kind, scope_group='*', scope_column='*')`
5. 未命中 → 记 unmatched，走 AI 兜底或人工放行

歧义规则：同一 key 命中多目标 → 整条丢弃（沿用 `build_index` 现有约定）。

### 2.3 代码接入点

- `Master.build_index`：加载 alias_scope，与 ports.aliases / carriers.aliases 合并
- `Master.expand_cn`：船司/POD/POL 分词后先查 alias_scope，未命中回退现有 port_idx
- `resolve_core.port_side`：同上
- 修 F7：unmatched 一律走软归一（记 self.unmatched），硬闸只在 validate_record 判"含说明文字"

### 2.4 迁移路径

从 11 份 markdown 缩写表提取，产出 `Database/migrations/seed_alias_scope_v1.py`：
- 逐群解析缩写表 → 按语义分三类 alias_kind
- 装不进 kind 8 值的（如"船期"）暂放 remark 或另立 kind
- 每条附 `evidence_msg_ids`（从群聊消息里找出处）
- 用户 review seed 脚本 → 一次性导入

---

## 3. L2 规则库：YAML 选型依据与结构

### 3.1 为什么选 YAML（不是 Python 常量，不是混合）

| 维度 | YAML | Python 常量 | 混合（YAML+编译缓存） |
|---|---|---|---|
| 数据量适配 | 每群 <100 行，解析开销可忽略 | 同 | 过度设计 |
| 非开发者可改 | ✅ 接近 markdown | ❌ 需懂 Python | ✅ |
| diff 友好 | ✅ | ✅ | ✅ |
| 改动生效 | 每次读文件即生效 | 需重启服务 | 需缓存失效逻辑 |
| schema 校验 | 需额外写 | 类型系统天然 | 需额外写 |
| 实现复杂度 | 低 | 低 | 高 |

**结论**：数据量小、用户会自己审校、需要快速迭代 → YAML 最合适。加载失败降级到"仅 AI 兜底"路径，不阻断服务。

### 3.2 目录结构

```
Database/rules/
  common.yaml                 # 全局规则（F6 去重目标）
  groups/
    宁波舱位滚动更新群.yaml
    CMA+EMC+PIL交流群.yaml
    ONE+ZIM+WHL+HPL+CSP交流群.yaml   # 只写 inherits: 华南-拉美 + 差异
    ...
  schema.yaml                 # JSON Schema，加载时校验
```

### 3.3 单份 YAML 结构

```yaml
# Database/rules/groups/宁波舱位滚动更新群.yaml
version: 1
inherits: null              # 或 "华南-拉美"，破 F8

templates:
  - name: 文字报价-滚动更新
    match:
      kind: space
      regex: '^(?P<carrier>[A-Z]{3,4})\s+(?P<vessel>[\u4e00-\u9fa5A-Z0-9/]+)'
    field_map:
      carrier: carrier
      vessel: vessel_voyage
  - name: 图片报价-表格
    match:
      has_image: true
    ocr_template: ningbo_space_table_v1

field_hints:
  pol:
    default: CNNGB
    fallback_if_missing: default_pol_from_group
  space_type:
    enum_ref: SPACE_TYPE_ENUM   # 引用代码里的唯一真源，不重复
  etd:
    parse_order: [iso, cn_date, weekday_offset]

business_rules:
  - id: ningbo_vessel_placeholder
    when: "vessel_voyage matches '^\\s*[（(].*[)）]\\s*$'"
    then: "reject: 船名航次占位符"
  - id: ningbo_space_type_首航
    when: "message_text contains '首航'"
    then: "set space_type = '首航'"

context_ref: Skill/freight-rate-heartbeat/references/群聊规范/宁波舱位滚动更新群/语境.md
```

### 3.4 common.yaml 去重目标（F6）

抽出全局规则：
- `附加费只进备注不自算`
- `港群存一条不拆成员`
- `default_pol 兜底不写全国基本港`
- `识别不到留空交人工`
- `逐字保留原文`
- `写 ocr_cache`

每条规则附 `applies_to_kinds: [rates, space]` 和 `evidence_ref: extraction-rules.md#锚点`。

### 3.5 加载与校验

```python
# Database/rules_loader.py（新增）
def load_rules(group_name: str) -> RulesBundle:
    """
    1. 读 common.yaml + groups/<group>.yaml
    2. JSON Schema 校验
    3. 处理 inherits 链（最多 3 层）
    4. 返回不可变 RulesBundle
    失败 → 记 log，返回 RulesBundle.empty()，让 AI 兜底
    """
```

启动时预加载全部群规则，任何一份校验失败即启动失败（早暴露）。运行时代码消费缓存，改动通过"重载规则"API 手动触发。

---

## 4. L3 语境卡：从"规范.md"到"语境.md"

### 4.1 保留内容

- 群定位（一句话）
- 发送人画像（谁常发什么，如"Dale万秋燕 固定发 MSK 包舱政策"）
- 群风格描述（如"报价密集、图片为主、文字简短"）
- 事故叙事（上一轮真实踩坑，供 AI 类比）

### 4.2 删除内容（迁到 L1/L2）

- 缩写表 → L1 alias_scope
- 抽取模板名与样例 → L2 templates
- 业务规则条目 → L2 business_rules
- 枚举值列举 → 删除（代码里已有）

### 4.3 目标体量

每份 <500 字（约 300-500 tokens），11 份合计 <6000 tokens。AI 兜底路径注入单份即可。

---

## 5. pending_records 审计轨迹（破 F5）

### 5.1 schema 变更

```sql
ALTER TABLE pending_records ADD COLUMN orig_payload TEXT;   -- 首次入库快照 JSON
ALTER TABLE pending_records ADD COLUMN edit_log TEXT;       -- JSON array
```

`edit_log` 条目格式：
```json
{
  "ts": "2026-09-14T15:30:00+08:00",
  "actor": "审核员A" | "agent_auto" | "candidate_applied",
  "action": "update" | "approve" | "reject",
  "field": "etd",
  "old": null,
  "new": "2026-09-21",
  "reason": "同发货人+船司历史模式"
}
```

### 5.2 代码变更

- `stage_pending`：INSERT 时 orig_payload = payload
- `update_pending`：追加 edit_log，不覆盖 orig_payload
- `approve_pending`：追加 edit_log（含最终入库行与 orig_payload 的 diff）
- `reject_pending`：追加 edit_log

### 5.3 迁移

现有 pending 行（2132 条）：
- `orig_payload = payload`（作为基线，无法回溯历史修改）
- `edit_log = []`
- 备注 `note` 追加 "[audit-baseline-2026-09-14]"

---

## 6. rule_candidates 演化机制

### 6.1 表结构

```sql
CREATE TABLE rule_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN (
    'sender+carrier+group',    -- 最窄，默认
    'sender+group',
    'carrier+group',
    'group+column',
    'group',
    'global'                    -- 最宽，需二次确认
  )),
  scope_key TEXT NOT NULL,      -- "张三|CMA|北美航线群"，用 '|' 分隔
  pattern_type TEXT NOT NULL CHECK (pattern_type IN (
    'alias',      -- 缩写映射 → L1
    'template',   -- 抽取模板 → L2
    'default',    -- 字段默认值 → L2
    'rule'        -- 业务规则 → L2
  )),
  target_layer TEXT NOT NULL CHECK (target_layer IN ('L1','L2','L3')),
  pattern_body TEXT NOT NULL,   -- JSON
  evidence_msg_ids TEXT,        -- JSON array
  evidence_count INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  confidence REAL,              -- 由 count + span 算
  source TEXT NOT NULL CHECK (source IN (
    'agent_observation',
    'audit_correction'
  )),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending','approved','rejected','expired'
  )),
  reviewer_note TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (scope_kind, scope_key, pattern_type, target_layer, pattern_body)
);

CREATE INDEX idx_candidates_status ON rule_candidates(status, confidence DESC);
```

### 6.2 confidence 公式

```
confidence = min(1.0, evidence_count / 10) × min(1.0, days_span / 7) × scope_weight

scope_weight:
  sender+carrier+group = 1.0    # 最窄最可信
  sender+group         = 0.9
  carrier+group        = 0.85
  group+column         = 0.7
  group                = 0.6
  global               = 0.4    # 全局最不可信，需人工二次确认
```

### 6.3 两条数据流

**agent_observation**：
- AI 兜底路径处理消息时，若"字典未命中但人工放行时补进 target"，记候选
- 触发点：`push_with_review` 后比对 orig_payload 与 approved payload
- 默认 scope：`sender+carrier+group`（用户指定最窄）

**audit_correction**：
- 审核员在 pending 界面改字段，追加 edit_log
- 后台任务每 6 小时扫 edit_log：同 (sender, carrier, group, field) 维度累计修改 ≥3 次且方向一致 → 生成候选
- 默认 scope：`sender+carrier+group`

### 6.4 放行闸门

- confidence ≥ 0.6 → 进"待放行"看板
- 人工点批准 → 按 target_layer 写入 L1/L2/L3
  - L1：INSERT INTO alias_scope，source='candidate_approved'
  - L2：追加到对应 YAML 文件（人工编辑 or 自动 append + git 记录）
  - L3：追加到语境.md
- 人工点拒绝 → status='rejected'，保留证据
- 超过 90 天未处理 → status='expired'

### 6.5 scope 放宽机制

候选默认最窄。若审核员认为可以放宽（如"这个缩写全群通用"），点"提升作用域"按钮：
- 弹二次确认，显示当前 evidence 涉及多少 sender/carrier
- 提升后 confidence 按新 scope_weight 重算
- 提升到 `global` 需二人复核（记 reviewed_by 双字段）

---

## 7. 分期实施（P0-P4）

每期独立跑通、独立回退。

### P0：基础修复（不改规范，只补代码）
- pending_records 加 orig_payload / edit_log 列（F5）
- 补 alias_scope 的 CRUD 代码，接入 MASTER_TABLES（F1）
- 修 unmatched 双路径语义（F7）
- **交付**：迁移脚本 + 手工验证 3 条 pending 修改路径都写 edit_log
- **回退**：新列可直接 DROP，不影响业务

### P1：L1 字典迁移
- alias_scope 扩 kind 到 8 值 + 加 alias_kind / scope_column / evidence_count（F2/F3）
- 从 11 份规范缩写表提取，产 seed 脚本
- 用户 review seed → 一次性导入
- 接入 expand_cn / resolve_core
- **交付**：seed_alias_scope_v1.py + 导入报告（每群迁移了多少条、多少条冲突）
- **回退**：DELETE FROM alias_scope WHERE source='migration_v1'

### P2：L2 规则库落地
- 建 Database/rules/ 目录 + schema.yaml
- common.yaml 抽 F6 全局规则
- 每群 YAML 迁移抽取模板 + 业务规则
- 新增 rules_loader.py + 启动预加载
- 接入 record_builder / push_with_review
- **交付**：11 份 groups/*.yaml + common.yaml + loader
- **回退**：loader 加"禁用 L2"开关，回退到纯 AI 路径

### P3：L3 语境卡瘦身
- 11 份规范.md → 语境.md（改名 + 删结构化内容）
- SKILL.md 主流程第 3 步改指向语境.md
- 删除与 extraction-rules.md 重复的全局规则
- **交付**：11 份语境.md（每份 <500 字）+ 迁移报告
- **回退**：保留规范.md.bak，SKILL.md 恢复指向

### P4：演化机制
- rule_candidates 表
- agent_observation 触发点（push_with_review 后比对）
- audit_correction 后台扫描任务（每 6 小时）
- 看板加"候选放行"tab
- **交付**：候选表 + 两条数据流 + 放行界面
- **回退**：候选表 DROP，看板 tab 隐藏

---

## 8. 代码/AI 分工调整

### 8.1 新流程

```
消息进入
  ↓
record_builder 模板直出（现状）
  ↓ 未命中
L1 字典查询（alias_scope）
  ↓ 命中 → 代码直出
  ↓ 未命中
L2 规则库匹配（templates + business_rules）
  ↓ 命中 → 代码直出
  ↓ 未命中或置信度低
AI 兜底（注入该群 L3 语境卡）
  ↓
代码闸门（validate_record + expand_cn + MAX_EXPAND）
  ↓
pending_records（写 orig_payload）
  ↓
人工审核（update_pending 写 edit_log）
  ↓
approve → push_with_review → 业务表
  ↓
比对 orig vs approved → agent_observation 候选
edit_log 累计 → audit_correction 候选
  ↓
rule_candidates → 人工放行 → L1/L2/L3
```

### 8.2 KPI

每月统计：
- **代码直出率** = (record_builder + L1 + L2 命中) / 总消息数
- **AI 兜底率** = AI 处理消息数 / 总消息数
- **人工修正率** = 审核员改动字段数 / 总字段数
- **候选转化率** = 放行候选数 / 生成候选数

目标：代码直出率 P0 后 60% → P2 后 75% → P4 后 85%。

---

## 9. 量化收益估算

假设日均 1000 条消息：

| 阶段 | AI 处理条数 | 单条 token | 日 token | 相对现状 |
|---|---|---|---|---|
| 现状（markdown 全规范按需读） | ~400（60% 直出） | ~2500（读 1-3KB 规范+消息） | 1.0M | 基线 |
| P1 后（L1 字典接入） | ~300（70% 直出） | ~2000 | 0.6M | -40% |
| P2 后（L2 规则库） | ~200（80% 直出） | ~1500 | 0.3M | -70% |
| P3 后（L3 瘦身） | ~200 | ~800（语境卡 <500 字） | 0.16M | -84% |
| P4 后（演化闭环） | ~150（85% 直出） | ~800 | 0.12M | -88% |

维护性收益：
- 全局规则改一处生效（F6 破除）
- alias_scope 可 SQL 查询/diff/lint
- 候选表提供规则演化溯源

---

## 10. 风险与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| YAML 加载失败 | 服务启动失败或规则失效 | schema 校验 + 启动预加载 + 失败即启动失败（早暴露）；运行时"禁用 L2"开关兜底 |
| 迁移期双源共存 | 规范.md 与 YAML 不一致 | P1/P2 期间代码优先读 YAML，缺失回退 markdown；每期结束删除已迁移的 markdown 段 |
| 现有 pending 数据丢失 | 2132 条待审核数据受影响 | P0 前 GET /api/export 存整库快照；orig_payload 迁移用当前 payload 作基线，明确标注"无法回溯历史" |
| 候选表膨胀 | rule_candidates 无限增长 | 90 天未处理自动 expired；同 scope+pattern_body 唯一约束防重复 |
| 审核员抵触新界面 | 反向沉淀数据流断供 | P4 前与审核员对齐；候选放行 tab 只做增量，不改现有 pending 界面 |
| ONE 群继承机制复杂 | YAML inherits 链循环 | loader 限最多 3 层继承，检测循环即报错 |

---

## 11. 未纳入本方案的现有隐患（标记 TODO）

- F10 Master 缓存非线程安全（`self.unmatched` 实例级 list）
- F9 `ensure_carrier` 脏值积压（已有 792 项候选清单，需独立治理）
- 舱位类型档数在 agent-input-contract.md 内部自相矛盾（108 行 11 档 vs 130 行八档）
- architecture.md:41 仍写"舱位类型只八档"，需同步
- DDL 与 _migrate 双份维护（新列既要 CREATE TABLE 又要 ALTER TABLE）
- canon_for_key 前导零丢失（`"08444"` → `"8444"`）
- normalize 里 raise ValueError 会中断整批（push_with_review:849 无 try/except）

这些是现有代码脆弱点，与规范重构正交，建议独立排期。

---

## 12. 待用户决策的开放问题

1. **P0 是否立即启动**：只补代码不改规范，风险最低，收益立竿见影（审计轨迹+死表复活）
2. **L2 YAML 目录放哪**：`Database/rules/` 还是 `Skill/freight-rate-heartbeat/rules/`？前者靠近代码，后者靠近规范
3. **ONE 群继承机制**：YAML `inherits: 华南-拉美` 是否可接受？还是保持每群独立完整（冗余但直白）
4. **候选表 confidence 阈值**：0.6 是否合适？太低会淹没审核员，太高会漏掉弱模式
5. **审核员界面改动幅度**：P4 加"候选放行"tab 是全新界面，还是嵌入现有 pending tab
6. **迁移期长度**：P1-P3 期间 markdown 与 YAML 并存，允许多长时间（1 周 / 1 个月 / 直到 P4 结束）

---

**方案结束。请 review 后回复：**
- 哪些决策直接同意
- 哪些需要调整（指出具体章节）
- 哪些开放问题需要进一步讨论

**批准前不动任何代码。**
