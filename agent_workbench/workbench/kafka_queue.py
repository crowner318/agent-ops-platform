"""Deliver committed SQLite dispatch records to Kafka by identifier only."""
import os
import threading
import time


DEFAULT_TOPIC = "agent.jobs.v1"
DEFAULT_BOOTSTRAP = "kafka:9092"
PROBE_INTERVAL = 5
PROBE_FRESHNESS = 15


def kafka_config(bootstrap_servers=None, topic=None):
    return (
        bootstrap_servers or os.environ.get("KAFKA_BOOTSTRAP_SERVERS", DEFAULT_BOOTSTRAP),
        topic or os.environ.get("KAFKA_TOPIC", DEFAULT_TOPIC),
    )


def topic_ready(client, topic):
    metadata = client.list_topics(timeout=3)
    entry = metadata.topics.get(topic)
    return bool(entry and not entry.error and entry.partitions and all(
        partition.leader >= 0 and not partition.error for partition in entry.partitions.values()
    ))


class OutboxPublisher:
    def __init__(self, store, *, bootstrap_servers=None, topic=None, producer=None):
        servers, self.topic = kafka_config(bootstrap_servers, topic)
        if producer is None:
            from confluent_kafka import Producer

            producer = Producer({
                "bootstrap.servers": servers,
                "client.id": "agent-workbench-outbox",
                "enable.idempotence": True,
                "acks": "all",
                "delivery.timeout.ms": 10000,
                "request.timeout.ms": 5000,
            })
        self.store = store
        self.producer = producer
        self._stop = threading.Event()
        self._thread = None
        self._last_ready = None

    @property
    def available(self):
        checked = self._last_ready
        return not self._stop.is_set() and checked is not None and time.monotonic() - checked < PROBE_FRESHNESS

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kafka-outbox", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _publish(self, event):
        delivery = []

        def acknowledged(error, _message):
            delivery.append(error)

        self.producer.produce(
            self.topic,
            key=event["task_id"].encode("ascii"),
            value=event["id"].encode("ascii"),
            on_delivery=acknowledged,
        )
        if self.producer.flush(10) or not delivery or delivery[0] is not None:
            raise RuntimeError("Kafka 未确认消息写入。")

    def publish_pending(self):
        for event in self.store.dispatch_pending():
            if self._stop.is_set():
                return
            try:
                self._publish(event)
            except Exception:
                self._last_ready = None
                raise
            self._last_ready = time.monotonic()
            # If this write fails, SQLite still owns the event. The next send
            # can deliver a duplicate, which is safe at the execution claim.
            self.store.dispatch_published(event["id"])

    def _run(self):
        next_probe = 0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + PROBE_INTERVAL
                try:
                    self._last_ready = time.monotonic() if topic_ready(self.producer, self.topic) else None
                except Exception:
                    self._last_ready = None
            if self.available:
                try:
                    self.publish_pending()
                except Exception:
                    self._stop.wait(2)
                    continue
            self._stop.wait(0.5)
