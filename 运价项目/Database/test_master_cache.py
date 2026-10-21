import threading
import time
import unittest
from unittest.mock import patch

import manualdb


WORKER_COUNT = 20


class MasterCacheTests(unittest.TestCase):
    def test_cold_cache_builds_master_once_for_concurrent_requests(self):
        calls = 0
        calls_lock = threading.Lock()
        barrier = threading.Barrier(WORKER_COUNT)

        class FakeMaster:
            ports = ["Shanghai"]

        def build_master(_conn):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.02)
            return FakeMaster()

        manualdb.invalidate_master()
        with patch("manualdb.Master", side_effect=build_master):
            def read_master():
                barrier.wait()
                return manualdb.get_master(object())

            threads = [threading.Thread(target=read_master) for _ in range(WORKER_COUNT)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(calls, 1)
        manualdb.invalidate_master()


if __name__ == "__main__":
    unittest.main()
