import unittest

from manual_server import FreightHTTPServer, SERVER_REQUEST_QUEUE_SIZE


class ServerCapacityTests(unittest.TestCase):
    def test_listener_backlog_is_sized_for_lan_sales_burst(self):
        self.assertEqual(FreightHTTPServer.request_queue_size, SERVER_REQUEST_QUEUE_SIZE)
        self.assertGreaterEqual(SERVER_REQUEST_QUEUE_SIZE, 128)


if __name__ == "__main__":
    unittest.main()
