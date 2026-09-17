# 这是什么 · 怎么用（人话版）

这个 skill 干一件事：**把若干钉钉群里发的海运运价/舱位信息，自动整理成结构化记录，送进「运价台账」的待审核队列**，由人工在看板点放行才真正入库。它不再写钉钉 AI 表格、也不再维护旧的本机看板。

## 一轮心跳怎么走

```
watermark(到哪了) → dws 逐群拉新消息 → 落盘原文 → record_builder 模板直出候选记录
   → 模板没命中/新图片 → LLM 补判(读图/引用回复) → 合成中文字段记录
   → /api/validate 预检 → /api/pending 暂存待审核 → 人工在看板放行 → 落业务表
```
只有"拉消息"和"下载图片"这两步靠宿主 `dws`（Agent 逐条 Bash）；其余全在 Python 里跑，提交是**本机 HTTP 直连台账**（不用逐条命令）。

## 目录

- `SKILL.md` —— 给 AI 看的主流程与口径（触发这个 skill 时读它）。
- `scripts/` —— 真正跑代码的地方（见 `scripts/Instruction.md`）。
- `scripts/config/` —— 两份配置（见 `scripts/config/Instruction.md`）。
- `references/` —— 按需查的细则文档（见 `references/Instruction.md`）。

## 运行态在哪（重点）

**运行态不在这个 skill 包里**，避免打包/安装把缓存和状态一起带走。三样东西都写到 `config/ledger_contract.json` 的 `runtime_dir` 指的那个目录（本机默认是 skill 同级的 `heartbeat-data/`）：

- `heartbeat-data/watermark.json` —— 每个群上次拉到哪一刻（水位线）。删了它 = 从"今天 00:00"重新拉。
- `heartbeat-data/.work/` —— 每轮的过程文件：`msgs_<日期>` 逐字消息、`draft_<日期>` 候选记录、`llm_out_<日期>` LLM 判定、`run_<日期>` 状态机。可随便删，下轮重算。
- `heartbeat-data/ocr_cache/` —— 图片识别结果缓存，**跨群复用别删**（同图只识一次）。

要换地方：改 `runtime_dir`，或设环境变量 `FRT_RUNTIME_DIR`。所有路径都由 `scripts/paths.py` 一处算，别在别处拼。

## 快速跑一遍

```bash
python scripts/paths.py            # 看解析出的目录对不对
python scripts/ledger_client.py    # 冒烟：能否连上台账 + 打一条预检
python scripts/watermark.py get    # 看各群水位
python scripts/pipeline.py next    # 出这一步该做的动作（dws 命令 / 提交）
python scripts/pipeline.py advance # 推进状态机，可反复跑到 DONE
```

台账服务（`Database/manual_server.py`，默认 `127.0.0.1:8795`）得先起来；写令牌在 `Database/token_manual.txt`。
