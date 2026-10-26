import logging
import threading
import uuid
from freight_flow.core.contracts import PULSE_SECONDS, POLL_SECONDS
from freight_flow.core.errors import AgentBlockedError

LOG = logging.getLogger("worker")


def work(store, adapter, stop, once=False, owner=None, gate=None, on_agent_block=None,
         before_claim=None):
    owner = owner or uuid.uuid4().hex
    while not stop.is_set():
        if gate is not None and not gate.is_set():
            stop.wait(POLL_SECONDS)
            continue
        if before_claim is not None:
            try:
                before_claim()
            except Exception:
                LOG.exception('[CONFIG] 任务领取前同步运行配置失败，暂停领取')
                if gate is not None:
                    gate.clear()
                stop.wait(POLL_SECONDS)
                continue
        try:
            task = store.claim(owner)
        except Exception:
            LOG.exception('[RECOVERY] 队列读取异常，执行线程保留并等待自动重试')
            if once:
                return
            stop.wait(POLL_SECONDS)
            continue
        if task is None:
            if once:
                return
            stop.wait(POLL_SECONDS)
            continue
        pulse_stop = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not pulse_stop.wait(PULSE_SECONDS):
                try:
                    if not store.pulse(task):
                        lost.set()
                        return
                except Exception:
                    LOG.exception("[%s] 心跳失败", task["run_id"])
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            result = adapter.execute(task, store.results(task["run_id"]))
            if lost.is_set():
                raise RuntimeError("worker 租约丢失，拒绝交付结果")
            store.finish(task, result=result)
            LOG.info("[%s] %s 已完成", task["run_id"], task["stage"])
        except AgentBlockedError as exc:
            LOG.error("[%s] %s: %s", task["run_id"], task["stage"], exc)
            try:
                store.finish(task, error=str(exc), retryable=False)
            except ValueError:
                LOG.warning("[%s] 已拒绝迟到的认证失败结果", task["run_id"])
            if on_agent_block is not None:
                try:
                    on_agent_block(str(exc), exc.health_state)
                except Exception:
                    LOG.exception("[%s] 认证失败后暂停 Worker 失败", task["run_id"])
        except Exception as exc:
            LOG.error("[%s] %s: %s", task["run_id"], task["stage"], exc)
            try:
                results = store.results(task['run_id'])
                repaired = isinstance(exc, ValueError) and store.repair(task, str(exc))
                # Once raw evidence is prepared, a model timeout must become a visible
                # evidence-only review item even when extract never produced records.
                technical_partial = not isinstance(exc, ValueError) and (
                    results.get('prepare') is not None or results.get('extract') is not None)
                message_partial = task.get('mode') == 'production-message'
                if not repaired and message_partial and hasattr(adapter, 'deliver_partial') and (
                        isinstance(exc, ValueError) or technical_partial):
                    try:
                        receipt = adapter.deliver_partial(task, results, str(exc))
                        if technical_partial:
                            receipt = {**receipt, 'failure_kind': 'technical'}
                        store.finish_partial(task, receipt, str(exc))
                        LOG.info("[%s] %s 已部分送审、读回并形成可见待核回执",
                                 task['run_id'], task['stage'])
                    except Exception as delivery_exc:
                        LOG.exception("[%s] 部分交付未形成完整回执，保留任务自动重试", task['run_id'])
                        store.finish(task, error='部分交付失败: ' + str(delivery_exc), retryable=True)
                elif not repaired:
                    if hasattr(adapter, 'report_failure'):
                        try:
                            adapter.report_failure(task, str(exc), results)
                        except Exception:
                            LOG.exception("[%s] %s 待核问题通知失败；任务仍标记待人工核对",
                                          task["run_id"], task["stage"])
                    store.finish(task, error=str(exc), retryable=not isinstance(exc, ValueError),
                                 terminal_status='NEEDS_REVIEW' if isinstance(exc, ValueError) else None)
            except ValueError:
                LOG.warning("[%s] 已拒绝迟到结果", task["run_id"])
        finally:
            pulse_stop.set()
            thread.join()
        if once:
            return
