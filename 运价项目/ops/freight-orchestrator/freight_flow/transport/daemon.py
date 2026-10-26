"""Single detached worker per control database; scheduler never waits for extraction."""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from freight_flow.core.contracts import PULSE_SECONDS

WORKER_HEALTH_SECONDS = PULSE_SECONDS * 3
START_TIMEOUT_SECONDS = PULSE_SECONDS
SERVICE_START_TIMEOUT_SECONDS = START_TIMEOUT_SECONDS * 2
SERVICE_STOP_TIMEOUT_SECONDS = START_TIMEOUT_SECONDS * 2
STATUS_POLL_SECONDS = 0.1
DETACHED_PROCESSES = []


def remember_detached(process):
    """Retain Popen wrappers so detached children are not reported as leaked by Python."""
    DETACHED_PROCESSES[:] = [item for item in DETACHED_PROCESSES if item.poll() is None]
    DETACHED_PROCESSES.append(process)


def poll_detached():
    DETACHED_PROCESSES[:] = [item for item in DETACHED_PROCESSES if item.poll() is None]


def read_status(runtime):
    path = Path(runtime) / 'worker-health.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def service_status(runtime):
    data = read_status(runtime)
    if not data:
        return {'state': 'stopped', 'healthy': False, 'reason': '无有效服务心跳'}
    try:
        heartbeat_at = float(data.get('at') or 0)
    except (TypeError, ValueError):
        return {'state': 'stale', 'healthy': False, 'reason': '服务心跳格式无效', 'data': data}
    age_seconds = max(0.0, time.time() - heartbeat_at)
    if age_seconds >= WORKER_HEALTH_SECONDS:
        return {'state': 'stale', 'healthy': False, 'age_seconds': age_seconds,
                'reason': '服务心跳已过期', 'data': data}
    if data.get('mode') == 'service':
        if not data.get('scheduler_alive', False):
            return {'state': 'degraded', 'healthy': False, 'age_seconds': age_seconds,
                    'reason': '调度线程未存活', 'data': data}
        if not data.get('workers_alive', False):
            return {'state': 'degraded', 'healthy': False, 'age_seconds': age_seconds,
                    'reason': 'Worker 线程不完整', 'data': data}
        return {'state': 'running', 'healthy': True, 'age_seconds': age_seconds, 'data': data}
    return {'state': data.get('mode') or 'worker', 'healthy': True,
            'age_seconds': age_seconds, 'data': data}


def healthy(runtime):
    return service_status(runtime)['healthy']


@contextmanager
def worker_lock(runtime):
    stream = (Path(runtime) / 'worker.lock').open('a+b')
    stream.seek(0, os.SEEK_END)
    if not stream.tell():
        stream.write(b'0')
        stream.flush()
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        yield False
        return
    try:
        yield True
    finally:
        stream.close()  # OS releases the lock on clean exit or process death.


def start_worker(entry, runtime, codex, model, dws):
    if healthy(runtime):
        return {'started': False, 'healthy': True}
    command = [sys.executable, str(entry), '--runtime', str(runtime), 'worker',
               '--codex', str(codex), '--model', model, '--dws', str(dws)]
    log = (Path(runtime) / 'worker.log').open('ab')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session': True}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        remember_detached(process)
    finally:
        log.close()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if healthy(runtime):
            return {'started': True, 'healthy': True, 'pid': process.pid}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError('独立worker未确认启动，任务已留在持久队列')


def start_service(entry, runtime, codex, model, dws, schedule, port, concurrency):
    current = service_status(runtime)
    if current['healthy']:
        return {'started': False, 'healthy': True, 'state': current['state'],
                'pid': (current.get('data') or {}).get('pid')}
    command = [sys.executable, str(entry), '--runtime', str(runtime), 'service',
               '--codex', str(codex), '--model', model, '--dws', str(dws),
               '--schedule', str(schedule), '--port', str(int(port)),
               '--concurrency', str(int(concurrency))]
    log = (Path(runtime) / 'service.log').open('ab')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session': True}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        remember_detached(process)
    finally:
        log.close()
    deadline = time.monotonic() + SERVICE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = service_status(runtime)
        current_pid = (current.get('data') or {}).get('pid')
        # Windows venv launchers may re-exec the base interpreter, so the
        # healthy service PID can be the child of the Popen wrapper.
        if current['healthy'] and current['state'] == 'running':
            return {'started': True, 'healthy': True, 'state': 'running', 'pid': current_pid}
        if process.poll() is not None:
            break
        time.sleep(STATUS_POLL_SECONDS)
    raise RuntimeError('常驻服务未确认启动，请检查 service.log；持久队列未被修改')


def stop_service(runtime):
    current = service_status(runtime)
    if current['state'] == 'stopped':
        return {'stopped': True, 'already_stopped': True}
    service_pid = (current.get('data') or {}).get('pid')
    stop_path = Path(runtime) / 'worker-stop.request'
    stop_path.write_text(json.dumps({'requested_at': time.time()}, ensure_ascii=False), encoding='utf-8')
    deadline = time.monotonic() + SERVICE_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if service_status(runtime)['state'] == 'stopped':
            for process in DETACHED_PROCESSES:
                if process.pid == service_pid:
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        raise RuntimeError('服务心跳已停止，但进程尚未退出；未强制终止进程')
                    break
            poll_detached()
            return {'stopped': True, 'already_stopped': False}
        time.sleep(STATUS_POLL_SECONDS)
    raise RuntimeError('停止请求已写入，但服务未在时限内确认退出；未强制终止进程')
