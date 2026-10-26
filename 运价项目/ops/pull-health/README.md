# Freight pull health sidecar

Windows Task Scheduler runs `monitor-pull-health.ps1` every five minutes in the collection window as the interactive YQN user. It reads scheduler state, `ops/codex-heartbeat/logs/heartbeat-*.md`, the configured group list, and `Skill/heartbeat-data/.work/run_*/pull_receipt_*.json`. It does not invoke DWS, Codex collection, the pipeline, or ledger write endpoints.

Only this sidecar writes `state/pull-health-state.json` and `state/pull-health-{ALERT,RECOVERED}-*.md`. The Codex read-only notification heartbeat reads that directory. These outputs are outside the collector log and parser input directories. A missing group or non-ok receipt status is an alert; an unchanged watermark alone is normal when there are no new messages.
