"""Exclusive listener ownership precedes any Agent job database changes."""
import os
import socket
import unittest
from unittest.mock import Mock, patch

from workbench.server import WorkbenchHTTPServer, create_server


class LocalService:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class ServerLifecycleTests(unittest.TestCase):
    def test_second_listener_cannot_share_endpoint_or_initialize_job_database(self):
        service = LocalService()
        first = create_server({}, port=0, service=service)
        try:
            with patch("workbench.service.WorkbenchService") as constructor:
                with self.assertRaises(OSError):
                    create_server({}, port=first.server_port)
                constructor.assert_not_called()
            self.assertEqual(0, service.closed)
            self.assertNotEqual(-1, first.socket.fileno())
            with socket.create_connection(("127.0.0.1", first.server_port), timeout=2):
                pass
        finally:
            first.server_close()
        self.assertEqual(1, service.closed)

    def test_bind_failure_does_not_take_ownership_of_injected_service(self):
        first_service, unused_service = LocalService(), LocalService()
        first = create_server({}, port=0, service=first_service)
        try:
            with self.assertRaises(OSError):
                create_server({}, port=first.server_port, service=unused_service)
            self.assertEqual(0, first_service.closed)
            self.assertEqual(0, unused_service.closed)
        finally:
            first.server_close()

    def test_service_constructor_failure_releases_previously_bound_socket(self):
        created = []

        def capture(*args, **kwargs):
            server = WorkbenchHTTPServer(*args, **kwargs)
            created.append(server)
            return server

        with patch("workbench.server.WorkbenchHTTPServer", side_effect=capture), \
             patch("workbench.service.WorkbenchService", side_effect=RuntimeError("test constructor failure")):
            with self.assertRaisesRegex(RuntimeError, "test constructor failure"):
                create_server({}, port=0)
        failed = created[0]
        self.assertEqual(-1, failed.socket.fileno())
        replacement = create_server({}, port=failed.server_port, service=LocalService())
        replacement.server_close()

    def test_server_close_is_idempotent_with_and_without_service(self):
        service = LocalService()
        server = create_server({}, port=0, service=service)
        server.server_close()
        server.server_close()
        self.assertEqual(1, service.closed)
        # TCPServer invokes this during a failed bind, before create_server can
        # attach a service. It must preserve the original binding exception.
        unowned = WorkbenchHTTPServer(("127.0.0.1", 0), Mock(), bind_and_activate=False)
        unowned.server_close()
        unowned.server_close()
        self.assertEqual(-1, unowned.socket.fileno())

    @unittest.skipUnless(os.name == "nt", "Windows-specific socket ownership")
    def test_windows_socket_is_exclusive_and_not_reusable(self):
        server = create_server({}, port=0, service=LocalService())
        try:
            self.assertFalse(server.allow_reuse_address)
            self.assertEqual(0, server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))
            self.assertNotEqual(0, server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE))
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
