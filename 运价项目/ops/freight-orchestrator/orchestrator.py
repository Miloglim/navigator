"""Persistent workflow entrypoint; replay cannot write business records."""
import argparse
import hashlib
import json
import logging
import shutil
import sys
import threading
import uuid
import time
from pathlib import Path
from freight_flow.core.contracts import PORT, SCHEDULED_SHARDS
from freight_flow.services.workflow import plan
from freight_flow.transport.store import Store
from freight_flow.transport.agent import Agent
from freight_flow.transport.worker import work
from freight_flow.transport.server import server
from freight_flow.transport.production import Production
from freight_flow.transport.production import atomic_json
from freight_flow.transport.daemon import (service_status, start_service, start_worker,
                                           stop_service, worker_lock)
from freight_flow.transport.engine import EngineService
from freight_flow.transport.runtime_control import RuntimeControl
from freight_flow.transport.runtime_settings import effective_settings
from freight_flow.core.contracts import PRODUCTION_MESSAGE_STAGES, PULSE_SECONDS
from freight_flow.core.contracts import STOP_FILE

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "Skill" / "heartbeat-data" / "orchestrator"
RUNTIME_SETTINGS = Path(__file__).parent / "config" / "runtime.json"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def enqueue_fixture(store, runtime, fixture, trigger_key, lane, mode='replay'):
    existing = store.trigger(trigger_key)
    if existing:
        return existing
    source = json.loads(fixture.read_text(encoding="utf-8-sig"))
    parse_media = source.get('parse_media_ids')
    if parse_media is None:
        parse_media = source.get('media_ids') or []
    stages = [s for s in PRODUCTION_MESSAGE_STAGES if s != 'vision' or parse_media or source.get('image_path')] if mode == 'production-message' else plan(source)
    if mode == 'production-message' and any(k not in source for k in ('group', 'message_time', 'message_id', 'text', 'group_knowledge')):
        raise ValueError('生产消息缺来源或群规范')
    # Persist before execution; a new trigger cannot overwrite an older run's evidence.
    run_id = uuid.uuid4().hex
    snapshot = runtime / "artifacts" / run_id
    snapshot.mkdir(parents=True)
    if source.get("image_path"):
        image = Path(source["image_path"])
        if not image.is_absolute():
            image = fixture.parent / image
        target = snapshot / ("source" + image.suffix)
        shutil.copyfile(image, target)
        source["image_path"] = str(target.resolve())
        source["image_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    (snapshot / "source.json").write_bytes(encoded)
    source["input_sha256"] = hashlib.sha256(encoded).hexdigest()
    return store.enqueue(trigger_key, lane, source, stages, run_id=run_id, mode=mode)


def configured_values(args, require_codex=False):
    values = effective_settings(
        RUNTIME_SETTINGS,
        model=getattr(args, 'model', None),
        concurrency=getattr(args, 'concurrency', None),
        codex_exe=getattr(args, 'codex', None),
        dws_exe=getattr(args, 'dws', None),
    )
    if require_codex and not values['codex_exe']:
        raise ValueError('Codex CLI 未配置；请在“运行 → Worker 配置”填写路径，或传入 --codex')
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue-replay")
    enqueue.add_argument("--fixture", type=Path, required=True)
    enqueue.add_argument("--trigger-key", required=True)
    enqueue.add_argument("--lane", default="replay")
    production = sub.add_parser('enqueue-production')
    production.add_argument('--shard', choices=SCHEDULED_SHARDS, required=True)
    production.add_argument('--only')
    production.add_argument('--message-id')
    production.add_argument('--trigger-key', required=True)
    message = sub.add_parser('enqueue-message')
    message.add_argument('--fixture', type=Path, required=True)
    message.add_argument('--trigger-key', required=True)
    message.add_argument('--lane', default='production-test')
    history = sub.add_parser('enqueue-history')
    history.add_argument('--only', required=True)
    history.add_argument('--history-start', required=True)
    history.add_argument('--history-end', required=True)
    history.add_argument('--batch-size', type=int, default=3)
    history.add_argument('--trigger-key', required=True)
    cancel_history = sub.add_parser('cancel-history')
    cancel_history.add_argument('--reason', default='用户取消历史回补，恢复实时水位拉取')
    worker = sub.add_parser("worker")
    worker.add_argument("--codex")
    worker.add_argument("--model")
    worker.add_argument("--once", action="store_true")
    worker.add_argument('--dws')
    start = sub.add_parser('start-worker')
    start.add_argument('--codex')
    start.add_argument('--dws')
    start.add_argument('--model')
    resume = sub.add_parser('resume')
    resume.add_argument('--run-id', required=True)
    resume.add_argument('--reread-source', action='store_true')
    resume.add_argument('--recheck-inventory', action='store_true')
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=PORT)
    serve.add_argument("--dws")
    serve.add_argument("--model")
    serve.add_argument("--schedule", type=Path, default=Path(__file__).parent / "config" / "schedule.json")
    serve.add_argument("--concurrency", type=int)
    service = sub.add_parser("service")
    service.add_argument("--codex")
    service.add_argument("--dws")
    service.add_argument("--model")
    service.add_argument("--schedule", type=Path, default=Path(__file__).parent / "config" / "schedule.json")
    service.add_argument("--port", type=int, default=PORT)
    service.add_argument("--concurrency", type=int)
    start_service_parser = sub.add_parser("start-service")
    start_service_parser.add_argument("--codex")
    start_service_parser.add_argument("--dws")
    start_service_parser.add_argument("--model")
    start_service_parser.add_argument("--schedule", type=Path, default=Path(__file__).parent / "config" / "schedule.json")
    start_service_parser.add_argument("--port", type=int, default=PORT)
    start_service_parser.add_argument("--concurrency", type=int)
    sub.add_parser("stop-service")
    sub.add_parser("service-status")
    sub.add_parser("status")
    sub.add_parser('maintain')
    args = parser.parse_args()
    # Resolve once before detached service handoff; child cwd must not change artifact identity.
    args.runtime = args.runtime.resolve()
    args.runtime.mkdir(parents=True, exist_ok=True)
    store = Store(args.runtime / "control.sqlite3")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    if args.command == "enqueue-replay":
        result = {"run_id": enqueue_fixture(store, args.runtime, args.fixture, args.trigger_key, args.lane)}
    elif args.command == 'enqueue-production':
        adapter = Production(ROOT, args.runtime, store, None)
        result = {'run_ids': adapter.enqueue(args.shard, args.trigger_key, args.only, args.message_id)}
    elif args.command == 'enqueue-message':
        result = {'run_id': enqueue_fixture(store, args.runtime, args.fixture, args.trigger_key, args.lane, 'production-message')}
    elif args.command == 'enqueue-history':
        adapter = Production(ROOT, args.runtime, store, None, dws=configured_values(args)['dws_exe'])
        result = {'run_id': adapter.enqueue_history(args.trigger_key, args.only, args.history_start,
                                                    args.history_end, args.batch_size)}
    elif args.command == 'cancel-history':
        result = store.cancel_history(args.reason)
    elif args.command == 'start-worker':
        configured = configured_values(args, require_codex=True)
        result = start_worker(Path(__file__).resolve(), args.runtime, configured['codex_exe'],
                              configured['model'], configured['dws_exe'])
    elif args.command == 'start-service':
        configured = configured_values(args, require_codex=True)
        result = start_service(Path(__file__).resolve(), args.runtime, configured['codex_exe'],
                               configured['model'], configured['dws_exe'], args.schedule.resolve(),
                               args.port, configured['concurrency'])
    elif args.command == 'stop-service':
        result = stop_service(args.runtime)
    elif args.command == 'service-status':
        result = service_status(args.runtime)
    elif args.command == 'resume':
        if args.reread_source and args.recheck_inventory:
            raise ValueError('只选择一种维护重核方式')
        if args.reread_source:
            store.recheck_source(args.run_id)
            result = {'reread_source': True}
        elif args.recheck_inventory:
            store.recheck_inventory(args.run_id)
            result = {'recheck_inventory': True}
        else:
            result = {'resumed_tasks': store.resume(args.run_id)}
    elif args.command == "status":
        result = store.runs()
    elif args.command == 'maintain':
        store.maintain()
        result = {'maintained': True}
    elif args.command == "serve":
        configured = configured_values(args)
        control = RuntimeControl(args.runtime, store, args.schedule.resolve(), dws=configured['dws_exe'],
                                 model=configured['model'], concurrency=configured['concurrency'],
                                 start_auth_monitor=True)
        server(store, Path(__file__).parent / "web" / "index.html", args.port,
               runtime_control=control).serve_forever()
        return
    elif args.command == "service":
        configured = configured_values(args, require_codex=True)
        agent = Agent(configured['codex_exe'], configured['model'], args.runtime / "artifacts")
        production = Production(ROOT, args.runtime, store, agent, configured['dws_exe'])
        with worker_lock(args.runtime) as acquired:
            if not acquired:
                raise RuntimeError("已有采集 worker/service 使用当前控制库")
            service = EngineService(args.runtime, store, production, args.schedule, configured['model'],
                                    concurrency=configured['concurrency'],
                                    workers_enabled=configured['workers_enabled'])
            service.serve_forever(Path(__file__).parent / "web" / "index.html", args.port)
        return
    else:
        configured = configured_values(args, require_codex=True)
        adapter = Agent(configured['codex_exe'], configured['model'], args.runtime / "artifacts")
        adapter = Production(ROOT, args.runtime, store, adapter, configured['dws_exe'])
        with worker_lock(args.runtime) as acquired:
            if not acquired:
                print(json.dumps({'ok': True, 'data': {'worker': 'already_running'}}))
                return
            stop = threading.Event()
            def health():
                while not stop.is_set():
                    if (args.runtime / STOP_FILE).is_file():
                        stop.set()
                        break
                    atomic_json(args.runtime / 'worker-health.json', {'pid': os.getpid(), 'at': time.time(), 'model': configured['model']})
                    stop.wait(PULSE_SECONDS)
            import os
            health_thread = threading.Thread(target=health, daemon=True)
            health_thread.start()
            workers = [threading.Thread(target=work, args=(store, adapter, stop, args.once), daemon=True)
                       for _ in range(1 if args.once else configured['concurrency'])]
            for w in workers:
                w.start()
            try:
                for w in workers:
                    w.join()
            except KeyboardInterrupt:
                stop.set()
                for w in workers:
                    w.join()
            finally:
                stop.set()
                health_thread.join()
                (args.runtime / 'worker-health.json').unlink(missing_ok=True)
                (args.runtime / STOP_FILE).unlink(missing_ok=True)
        result = {"worker": "stopped"}
    print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
