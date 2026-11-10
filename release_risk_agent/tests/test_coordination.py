"""Redis tests inject fakeredis through real redis-py APIs, not a live service."""
import json
import unittest
from unittest.mock import patch

import fakeredis

from release_guard.coordination import CoordinationError, LocalCoordinator, RedisCoordinator


class LocalCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.coordinator = LocalCoordinator(clock=lambda: self.now[0], pending_timeout=10)
        self.addCleanup(self.coordinator.close)

    def test_cache_ttl_and_detached_json(self):
        value = {"evidence": [{"id": "metric-1"}]}
        self.coordinator.set_cache("scope/tool", value, ttl=5)
        value["evidence"].clear()
        cached = self.coordinator.get_cache("scope/tool")
        self.assertEqual(cached["evidence"][0]["id"], "metric-1")
        cached["evidence"].clear()
        self.assertTrue(self.coordinator.get_cache("scope/tool")["evidence"])
        self.now[0] += 6
        self.assertIsNone(self.coordinator.get_cache("scope/tool"))
        self.assertEqual(self.coordinator.stats()["backend"], "local")
        self.assertEqual(self.coordinator.stats()["durability"], "process_memory_only")

    def test_lock_owner_and_expired_holder_cannot_release_new_lock(self):
        first = self.coordinator.acquire_lock("CHG-001", ttl=5)
        self.assertIsNone(self.coordinator.acquire_lock("CHG-001", ttl=5))
        self.assertFalse(self.coordinator.release_lock("CHG-001", "wrong"))
        self.now[0] += 6
        second = self.coordinator.acquire_lock("CHG-001", ttl=5)
        self.assertNotEqual(first, second)
        self.assertFalse(self.coordinator.release_lock("CHG-001", first))
        self.assertTrue(self.coordinator.release_lock("CHG-001", second))

    def test_queue_requires_ack_and_recovers_abandoned_pending(self):
        job_id = self.coordinator.enqueue("CHG-001")
        self.assertEqual(self.coordinator.claim("worker-1"), {"job_id": job_id, "change_id": "CHG-001"})
        self.assertIsNone(self.coordinator.claim("worker-2"))
        self.assertEqual(self.coordinator.stats()["queue_pending"], 1)
        self.now[0] += 11
        self.assertEqual(self.coordinator.claim("worker-2")["job_id"], job_id)
        self.assertTrue(self.coordinator.ack(job_id))
        self.assertFalse(self.coordinator.ack(job_id))
        self.assertIsNone(self.coordinator.claim("worker-2"))


class RedisCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.server = fakeredis.FakeServer()
        self.client = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.coordinator = RedisCoordinator("redis://localhost:6379/0", namespace="tests", client=self.client,
                                            pending_idle_ms=100)

    def test_cache_uses_redis_json_ttl_and_hashed_nonsecret_keys(self):
        key = "redis://user:must-never-be-in-key@localhost/tool"
        self.coordinator.set_cache(key, {"rows": [{"id": "e-1"}]}, ttl=5)
        cached = self.coordinator.get_cache(key)
        cached["rows"].clear()
        self.assertTrue(self.coordinator.get_cache(key)["rows"])
        redis_keys = self.client.keys("tests:cache:*")
        self.assertEqual(len(redis_keys), 1)
        self.assertNotIn("must-never-be-in-key", redis_keys[0])
        self.assertGreater(self.client.pttl(redis_keys[0]), 0)
        self.client.delete(redis_keys[0])
        self.assertIsNone(self.coordinator.get_cache(key))
        self.assertEqual(self.coordinator.stats()["cache_hits"], 2)
        self.assertEqual(self.coordinator.stats()["cache_misses"], 1)

    def test_redis_nx_and_transaction_lock_ownership(self):
        token = self.coordinator.acquire_lock("CHG-001", ttl=5)
        self.assertIsNone(self.coordinator.acquire_lock("CHG-001", ttl=5))
        self.assertFalse(self.coordinator.release_lock("CHG-001", "wrong"))
        self.assertFalse(self.coordinator.release_lock("not-locked", None))
        lock_name = self.client.keys("tests:lock:*")[0]
        self.assertGreater(self.client.pttl(lock_name), 0)
        self.client.delete(lock_name)  # simulate the old lease expiring
        replacement = self.coordinator.acquire_lock("CHG-001", ttl=5)
        self.assertFalse(self.coordinator.release_lock("CHG-001", token))
        self.assertEqual(self.client.get(lock_name), replacement)
        with patch.object(self.client, "pipeline", wraps=self.client.pipeline) as pipeline:
            self.assertTrue(self.coordinator.release_lock("CHG-001", replacement))
            pipeline.assert_called_once()

    def test_real_stream_group_commands_pending_recovery_and_ack(self):
        job_id = self.coordinator.enqueue("CHG-001")
        first = self.coordinator.claim("worker-1")
        self.assertEqual(first, {"job_id": job_id, "change_id": "CHG-001"})
        self.assertEqual(self.client.xpending("tests:queue", "review_workers")["pending"], 1)
        self.assertIsNone(self.coordinator.claim("worker-2"))
        self.client.xclaim("tests:queue", "review_workers", "worker-1", 0, [job_id], idle=1000)
        restarted = RedisCoordinator("redis://localhost:6379/0", namespace="tests",
                                      client=fakeredis.FakeRedis(server=self.server, decode_responses=True), pending_idle_ms=100)
        recovered = restarted.claim("worker-2")
        self.assertEqual(recovered, first)
        self.assertTrue(restarted.ack(job_id))
        self.assertFalse(restarted.ack(job_id))
        self.assertEqual(restarted.stats()["queue_pending"], 0)
        self.assertIsNone(restarted.claim("worker-2"))

    def test_byte_responses_follow_same_client_path(self):
        binary_client = fakeredis.FakeRedis(server=self.server, decode_responses=False)
        coordinator = RedisCoordinator("redis://localhost/0", namespace="binary", client=binary_client)
        coordinator.set_cache("key", {"ok": True})
        self.assertEqual(coordinator.get_cache("key"), {"ok": True})
        token = coordinator.acquire_lock("key")
        self.assertTrue(coordinator.release_lock("key", token))
        job_id = coordinator.enqueue("CHG-002")
        self.assertEqual(coordinator.claim("worker")["job_id"], job_id)
        self.assertTrue(coordinator.ack(job_id))

    def test_stale_pending_beyond_initial_scan_page_is_eventually_recovered(self):
        ids = [self.coordinator.enqueue(f"CHG-{index:03}") for index in range(15)]
        for _ in ids:
            self.assertIsNotNone(self.coordinator.claim("active-worker"))
        self.client.xclaim("tests:queue", "review_workers", "active-worker", 0, [ids[-1]], idle=1000)
        recovered = None
        for _ in range(5):
            recovered = self.coordinator.claim("recovery-worker")
            if recovered:
                break
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["job_id"], ids[-1])
        self.assertTrue(self.coordinator.ack(ids[-1]))

    def test_unavailable_service_never_falls_back_or_leaks_connection_secret(self):
        unavailable = fakeredis.FakeServer()
        unavailable.connected = False
        with self.assertRaises(CoordinationError) as result:
            RedisCoordinator("redis://user:never-show-this@localhost/0", client=fakeredis.FakeRedis(server=unavailable))
        self.assertNotIn("never-show-this", str(result.exception))
        self.assertIn("不会自动降级", str(result.exception))
        for value in ("file://localhost/0", "not a redis URL", None):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                RedisCoordinator(value, client=self.client)

    def test_invalid_json_and_ttls_fail_before_commands(self):
        for ttl in (0, -1, float("nan"), True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                self.coordinator.set_cache("key", {}, ttl)
        with self.assertRaises(ValueError):
            self.coordinator.set_cache("key", {"usage": float("inf")})
        with self.assertRaises(ValueError):
            self.coordinator.enqueue("invalid change with spaces")


if __name__ == "__main__":
    unittest.main()
