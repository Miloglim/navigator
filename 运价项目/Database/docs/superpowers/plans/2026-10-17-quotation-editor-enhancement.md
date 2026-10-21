# 报价表增强 Implementation Plan

> **For agentic workers:** Execute this plan inline in the current session with the existing project workflow.

**Goal:** 完成舱位选择加入报价表、正文编辑、复制尺寸记忆、行拖动和可选相邻单元格合并。

**Architecture:** 继续使用 `web/index.html` 中现有的账号隔离报价草稿 `quotation-editor-v1:<user_id>`。正文和格式开关作为草稿字段保存，运价与舱位行保持独立，渲染层根据开关生成合并展示和复制 HTML。

**Tech Stack:** 原生 HTML/CSS/JavaScript、localStorage、现有报价表选择 Map、现有静态页面检查脚本。

**Spec:** 2026-10-17 用户确认的报价表增强方案。

## Global Constraints

- 不新增依赖。
- 账号草稿必须按当前登录账号隔离。
- 原始行数据始终独立保存，合并只影响展示和复制。
- 舱位与运价分表、分排序、分合并。
- HTML 正文只保存安全的编辑结果，复制时同时提供 HTML 和纯文本。

---

### Task 1: 草稿状态与舱位选择

**Files:** `web/index.html`, `test_quotation_editor.cjs`

- [ ] 扩展草稿默认值：`bodyHtml`、`bodyText`、`mergeCells`、`spaceLayoutVersion`。
- [ ] 选择/取消舱位时同步 `space.rows`，刷新报价表计数和页面。
- [ ] 增加回归检查，确保舱位可进入报价表且账号隔离。

### Task 2: 正文编辑区

**Files:** `web/index.html`, `test_quotation_editor.cjs`

- [ ] 在运价表前、舱位表后渲染正文编辑区。
- [ ] 增加选区浮动工具栏，支持加粗、斜体、下划线、颜色、字号、对齐、列表、链接、撤销和重做。
- [ ] 使用 `contenteditable` 保存 HTML 与纯文本，清理脚本和事件属性。

### Task 3: 尺寸记忆、拖行和合并

**Files:** `web/index.html`, `test_quotation_editor.cjs`

- [ ] 复制尺寸继续写入当前账号草稿，默认中档 85%。
- [ ] 为运价和舱位表增加行拖动手柄与 `dragstart/drop` 排序。
- [ ] 增加合并开关，开启后只合并同表内相邻且值完全相同的单元格。
- [ ] 编辑合并单元格时先拆解为独立展示，数据仍保持逐行。

### Task 4: 验证

- [ ] 运行 `js_check.py`。
- [ ] 运行报价表单元测试和报价使用记录测试。
- [ ] 请求正式 8795 页面，确认新字段、舱位按钮、正文工具栏和合并开关已下发。
