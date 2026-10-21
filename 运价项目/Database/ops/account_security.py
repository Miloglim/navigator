"""Local login admission limits; business ownership is a display filter."""
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock
import time

from core.contracts import (LOGIN_ACCOUNT_ATTEMPTS, LOGIN_IP_ATTEMPTS, LOGIN_WINDOW_SECONDS,
    LOGIN_MAX_TRACKED_KEYS, LOGIN_MAX_INFLIGHT, LOGIN_RETRY_SECONDS)


class LoginThrottled(Exception):
    def __init__(self, retry_seconds, busy=False):
        super().__init__('登录尝试过于频繁，请稍后再试')
        self.retry_seconds = retry_seconds
        self.code = "login_busy" if busy else "login_throttled"


class LoginLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.attempts = {}
        self.slots = BoundedSemaphore(LOGIN_MAX_INFLIGHT)

    @contextmanager
    def admit(self, name, address):
        stamp = self.clock()
        keys = [('account', name), ('address', address)]
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if stamp - v[0] < LOGIN_WINDOW_SECONDS}
            for key, limit in zip(keys, (LOGIN_ACCOUNT_ATTEMPTS, LOGIN_IP_ATTEMPTS)):
                first, count = self.attempts.get(key, (stamp, 0))
                if count >= limit:
                    raise LoginThrottled(max(1, int(LOGIN_WINDOW_SECONDS - (stamp - first)) + 1))
            if len(self.attempts) + sum(k not in self.attempts for k in keys) > LOGIN_MAX_TRACKED_KEYS:
                raise LoginThrottled(LOGIN_RETRY_SECONDS)
            if not self.slots.acquire(blocking=False):
                raise LoginThrottled(LOGIN_RETRY_SECONDS, busy=True)
            for key in keys:
                first, count = self.attempts.get(key, (stamp, 0))
                self.attempts[key] = (first, count + 1)
        try:
            yield
        finally:
            self.slots.release()
