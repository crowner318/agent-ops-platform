from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call

from workbench.kafka_queue import OutboxPublisher, topic_ready
from workbench.kafka_worker import consume, execute_event


class Stop:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, _seconds):
        return self.stopped


class KafkaOutboxTests(TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.dispatch_pending.return_value = [{
            "id": "event-a", "task_id": "task-a", "action": {"review_digest": "private"},
        }]
        self.producer = Mock()
        self.publisher = OutboxPublisher(self.store, topic="test.jobs", producer=self.producer)

    def _delivery(self, error=None, pending=0):
        def flush(_timeout):
            callback = self.producer.produce.call_args.kwargs["on_delivery"]
            callback(error, None)
            return pending

        self.producer.flush.side_effect = flush

    def test_only_identifier_is_published_and_ack_precedes_sqlite_update(self):
        self._delivery()
        self.store.dispatch_published.side_effect = lambda _event_id: self.producer.flush.assert_called_once()
        self.publisher.publish_pending()
        self.assertEqual(self.producer.produce.call_args.args, ("test.jobs",))
        self.assertEqual(self.producer.produce.call_args.kwargs["key"], b"task-a")
        self.assertEqual(self.producer.produce.call_args.kwargs["value"], b"event-a")
        self.store.dispatch_published.assert_called_once_with("event-a")

    def test_failed_delivery_leaves_outbox_row_pending(self):
        self._delivery(error=RuntimeError("broker refused"))
        with self.assertRaises(RuntimeError):
            self.publisher.publish_pending()
        self.store.dispatch_published.assert_not_called()
        self.assertFalse(self.publisher.available)

    def test_retry_after_lost_database_ack_preserves_dispatch_identity(self):
        self._delivery()
        self.store.dispatch_published.side_effect = [RuntimeError("storage timeout"), None]
        with self.assertRaises(RuntimeError):
            self.publisher.publish_pending()
        self.publisher.publish_pending()
        self.assertEqual(self.store.dispatch_published.call_args_list, [call("event-a"), call("event-a")])
        self.assertEqual([attempt.kwargs["value"] for attempt in self.producer.produce.call_args_list], [b"event-a", b"event-a"])

    def test_readiness_requires_configured_topic_and_partition_leader(self):
        metadata = SimpleNamespace(topics={})
        self.producer.list_topics.return_value = metadata
        self.assertFalse(topic_ready(self.producer, "test.jobs"))
        partition = SimpleNamespace(leader=-1, error=None)
        metadata.topics["test.jobs"] = SimpleNamespace(error=None, partitions={0: partition})
        self.assertFalse(topic_ready(self.producer, "test.jobs"))
        partition.leader = 0
        self.assertTrue(topic_ready(self.producer, "test.jobs"))


class KafkaWorkerTests(TestCase):
    def setUp(self):
        self.store = Mock()
        self.run_task = Mock()
        self.stop = Stop()
        self.claim = {"claimed": True, "task_id": "task-a", "action": {"action": "start"}}
        self.store.dispatch_claim.return_value = self.claim

    def test_duplicate_delivery_does_not_execute_or_finish_another_claim(self):
        self.store.dispatch_claim.return_value = {"claimed": False}
        self.assertTrue(execute_event("event-a", self.store, self.run_task, "worker-a", self.stop))
        self.run_task.assert_not_called()
        self.store.dispatch_finish.assert_not_called()

    def test_finish_transport_retry_does_not_repeat_model_execution(self):
        self.store.dispatch_finish.side_effect = [RuntimeError("storage timeout"), None]
        self.assertTrue(execute_event("event-a", self.store, self.run_task, "worker-a", self.stop))
        self.run_task.assert_called_once_with("task-a", {"action": "start"})
        self.store.dispatch_claim.assert_called_once()
        self.assertEqual(2, self.store.dispatch_finish.call_count)

    def test_ambiguous_claim_is_retried_without_running_unowned_work(self):
        self.store.dispatch_claim.side_effect = [RuntimeError("reply lost"), {"claimed": False}]
        self.assertTrue(execute_event("event-a", self.store, self.run_task, "worker-a", self.stop))
        self.run_task.assert_not_called()

    def test_poison_event_is_skipped_and_offset_commit_is_explicit(self):
        consumer = Mock()
        malformed = Mock()
        malformed.error.return_value = None
        malformed.value.return_value = b"not an identifier"
        consumer.poll.return_value = malformed

        def committed(**_):
            self.stop.set()
            return [SimpleNamespace(error=None)]

        consumer.commit.side_effect = committed
        consume(consumer, self.store, self.run_task, "worker-a", self.stop)
        self.store.dispatch_claim.assert_not_called()
        consumer.commit.assert_called_once_with(message=malformed, asynchronous=False)

    def test_shutdown_before_finish_does_not_commit_offset(self):
        consumer = Mock()
        message = Mock()
        message.error.return_value = None
        message.value.return_value = b"event-a"
        consumer.poll.return_value = message
        self.run_task.side_effect = lambda *_: self.stop.set()
        self.store.dispatch_finish.side_effect = RuntimeError("storage timeout")
        consume(consumer, self.store, self.run_task, "worker-a", self.stop)
        self.store.dispatch_finish.assert_called_once()
        consumer.commit.assert_not_called()
