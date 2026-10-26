# Navigator external-Agent intake contract v1

## Endpoint

```http
POST https://<navigator-host>/api/agent/intake/v1
Authorization: Bearer <NAVIGATOR_AGENT_API_TOKEN>
Content-Type: application/json; charset=utf-8
```

The server decides the runtime host and port. External callers use the HTTPS reverse-proxy URL. Do not call a loopback address from another machine.

## Request envelope

```json
{
  "schema_version": "1.0",
  "kind": "rates",
  "agent": { "name": "external-parser", "version": "1.0.0" },
  "preserve_other_sources": true,
  "preserve_existing_source_rows": false,
  "records": []
}
```

Rules:

- `schema_version` is exactly `1.0`.
- `kind` is `rates`, `space`, or `group_suggestion`.
- `agent.name` is required by policy and limited to 128 characters; `version` is optional and limited to 64.
- `records` contains 1-1000 objects.
- Every record must contain non-empty `来源群`, `消息ID`, and the verbatim `消息原文`.
- `preserve_other_sources` defaults to `true`; `preserve_existing_source_rows` defaults to `false`.
- `origin` is server-controlled as `agent`; callers must not send it.

## Rate fields

Required: `起运港`, `目的港`, `船司`, `柜型`, `海运费USD`, and at least one of `有效期起` / `有效期止`.

| Field | Value |
|---|---|
| `起运港`, `目的港` | Canonical port name or registered port-group name |
| `船司` | Canonical carrier code such as `MAERSK`, `CMA`, `MSC` |
| `航线` | Canonical route name when explicitly known |
| `柜型` | `20GP`, `40HQ/HC`, `40NOR`, `45HQ/HC`; input `40GP`, `40DC`, `40DV` belongs to `40HQ/HC` |
| `海运费USD` | Non-negative integer, no currency symbol, separator, or decimal |
| `有效期起`, `有效期止` | `YYYY-MM-DD` |
| `船期ETD` | `YYYY-MM-DD` when explicitly known |
| `T/T航程`, `目免`, `亏舱费` | Non-negative integers |
| `AI判断`, `备注`, `发送人` | Optional text; use `AI判断=是` only for the documented validity fallback |
| `图片名`, `运价图片` | Optional evidence filename/reference |

## Space fields

Required: `起运港`, `船司`, at least one of `目的港` / `航线` / `航线服务`, and at least one of `有效期起` / `有效期止`.

Allowed optional fields include `目的港`, `航线`, `航线服务`, `船名航次`, `舱位类型`, `柜型`, `箱量`, `箱型箱量`, `船期截关`, `船期ETD`, `价格USD`, `T/T航程`, `AI判断`, `备注`, `发送人`, `图片名`, `舱位图`.

`舱位类型` must be one of `现舱`, `加班船`, `放舱开放`, `售罄`, `舱位紧张`, `截关截单`, `撤载改期`, `箱子动态`, `空班`, `跳港`, `首航`, or empty.

## Group suggestion fields

Use `kind=group_suggestion` with `港群`, `标签`, `类型` (`pol` or `pod`), comma-separated canonical `成员`, plus the mandatory source fields. The suggestion is reviewed before it can change master data.

## Success response

```json
{
  "ok": true,
  "data": {
    "schema_version": "1.0",
    "kind": "rates",
    "submitted_by": "external-parser",
    "staged": 1,
    "ids": [101],
    "entry_ids": ["entry-id"],
    "check_passed": 1,
    "check_failed": 0,
    "validation_failures": [],
    "coverage": { "ok": true, "missing": [] }
  }
}
```

`staged` means accepted into the human-review queue. It does not mean approved or visible to sales.

## Error response

```json
{
  "ok": false,
  "error": {
    "code": "invalid_request",
    "message": "records[0] missing 消息ID"
  }
}
```

HTTP `400` is an invalid envelope or field shape, `401` is a missing/invalid Agent token, `413` is an oversized body, and `5xx` is temporary transport/service failure. Retry `5xx` or connection failures with the same payload; do not retry `400` unchanged.
