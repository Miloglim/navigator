---
name: navigator-agent-intake
description: Submit already-parsed DingTalk group-chat freight rates or space records to Navigator's versioned external-Agent intake API. Use when an external Agent has canonical Chinese-field JSON and needs to stage records for human review; do not use for raw DWS pulls, OCR, or direct release.
---

# Navigator Agent Intake

Submit **parsed, structured records** to Navigator. The endpoint only stages records for human review; it never writes the formal rate or space tables directly.

## Required configuration

Set these values outside the conversation and skill files:

```text
NAVIGATOR_AGENT_API_URL=https://<navigator-host>/api/agent/intake/v1
NAVIGATOR_AGENT_API_TOKEN=<separate agent token configured on the server>
```

Never place a real token in chat, JSON payloads, source control, or logs.

## Workflow

1. Parse the group-chat message outside this skill. Preserve `来源群`, `消息ID`, and the verbatim `消息原文` on every record.
2. Read [the intake contract](references/intake-contract.md) and use canonical Chinese field names and values.
3. Build one version-1 envelope with `kind`, optional Agent identity, and one or more records.
4. Submit with the helper:

   ```bash
   python scripts/submit_intake.py --file parsed.json
   ```

   The helper reads the URL and Bearer token only from the two environment variables above.
5. Treat `{ "ok": true, "data": ... }` as **staged for review**, not as released or approved data. Show operators `staged`, `entry_ids`, `check_failed`, and `validation_failures`.
6. Retry only transport failures (`5xx`, timeout, connection loss) with the exact same payload and message IDs. Never change field values merely to make validation pass.

## Hard boundaries

- One record must contain one port product; split multi-port or conflicting products into separate records.
- Do not call `/api/push`, `/api/record/*`, approval routes, or `/api/pending`.
- Do not submit raw OCR text as structured fields when parsing failed. Use the normal Navigator review-issue flow instead; this skill only handles the parsed JSON stage.
- Do not claim a price is released until a human reviewer approves it in Navigator.
