"""Explicit local memory or real redis-py cache, locks and Streams queues.

Neither locks nor queues are the business source of truth; SQLite review IDs
must make redelivered jobs idempotent. No silent local fallback is permitted.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4


class CoordinationError(RuntimeError):
    pass


def _ttl(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("有效期必须为正数秒。")
    return value


def _key(value):
    if not isinstance(value, str) or not value or len(value) > 65536:
        raise ValueError("协调键必须为非空字符串且不超过65536字符。")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_json(value):
    if not isinstance(value, dict):
        raise ValueError("缓存值必须是JSON对象。")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("缓存数据超过2 MiB上限。")
    return raw


def _change(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("变更ID格式无效。")
    return value


def _decode(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


class LocalCoordinator:
    """Same-process memory only: neither distributed nor restart-durable."""
    def __init__(self, *, clock=time.monotonic, pending_timeout=60):
        self._clock, self._pending_timeout = clock, _ttl(pending_timeout)
        self._cache, self._locks, self._jobs = {}, {}, {}
        self._condition = threading.Condition(threading.RLock())
        self._hits = self._misses = 0
        self._closed = False

    def _check(self):
        if self._closed:
            raise CoordinationError("本地协调器已关闭。")

    def get_cache(self, key):
        key = _key(key)
        with self._condition:
            self._check()
            cached = self._cache.get(key)
            if cached and cached[0] > self._clock():
                self._hits += 1
                return json.loads(cached[1])
            self._cache.pop(key, None)
            self._misses += 1
            return None

    def set_cache(self, key, value, ttl=300):
        key, ttl, raw = _key(key), _ttl(ttl), _cache_json(value)
        with self._condition:
            self._check()
            self._cache[key] = (self._clock() + ttl, raw)

    def acquire_lock(self, key, ttl=900):
        key, ttl = _key(key), _ttl(ttl)
        with self._condition:
            self._check()
            lock = self._locks.get(key)
            if lock and lock[0] > self._clock():
                return None
            token = uuid4().hex
            self._locks[key] = (self._clock() + ttl, token)
            return token

    def release_lock(self, key, token):
        key = _key(key)
        with self._condition:
            self._check()
            lock = self._locks.get(key)
            if not lock or lock[0] <= self._clock() or lock[1] != token:
                return False
            del self._locks[key]
            return True

    def enqueue(self, change_id):
        change_id = _change(change_id)
        with self._condition:
            self._check()
            job_id = "local-" + uuid4().hex
            self._jobs[job_id] = {"change_id": change_id, "claimed_at": None, "consumer": None}
            self._condition.notify()
            return job_id

    def claim(self, consumer, wait_ms=0):
        _change(consumer)
        if type(wait_ms) is not int or not 0 <= wait_ms <= 60000:
            raise ValueError("等待时间必须为0至60000毫秒。")
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            while True:
                self._check()
                now = self._clock()
                for job_id, job in self._jobs.items():
                    if job["claimed_at"] is None or now - job["claimed_at"] >= self._pending_timeout:
                        job.update(claimed_at=now, consumer=consumer)
                        return {"job_id": job_id, "change_id": job["change_id"]}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, self._pending_timeout))

    def ack(self, job_id):
        with self._condition:
            self._check()
            job = self._jobs.get(job_id)
            if not job or job["claimed_at"] is None:
                return False
            del self._jobs[job_id]
            return True

    def stats(self):
        with self._condition:
            self._check()
            now = self._clock()
            return {"backend": "local", "durability": "process_memory_only", "cache_hits": self._hits,
                    "cache_misses": self._misses, "cache_entries": sum(row[0] > now for row in self._cache.values()),
                    "active_locks": sum(row[0] > now for row in self._locks.values()),
                    "queue_total": len(self._jobs), "queue_pending": sum(row["claimed_at"] is not None for row in self._jobs.values())}

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class RedisCoordinator:
    def __init__(self, redis_url, namespace="release_guard", client=None, *, pending_idle_ms=60000):
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace):
            raise ValueError("Redis命名空间格式无效。")
        if type(pending_idle_ms) is not int or pending_idle_ms < 1:
            raise ValueError("pending重领时间必须为正数毫秒。")
        try:
            parsed = urlsplit(redis_url)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CoordinationError("Redis连接地址格式无效；不会自动降级为本地模式。") from None
        try:
            import redis
            self._redis = redis
            self._client = client if client is not None else redis.Redis.from_url(
                redis_url, decode_responses=True, socket_timeout=65, socket_connect_timeout=5)
            self._owned_client = client is None
            self._namespace, self._group = namespace, "review_workers"
            self._stream = namespace + ":queue"
            self._pending_idle_ms = pending_idle_ms
            self._hits = self._misses = 0
            self._metrics_lock = threading.Lock()
            self._claim_cursor = "0-0"
            self._claim_lock = threading.Lock()
            self._client.ping()
            try:
                self._client.xgroup_create(self._stream, self._group, id="0-0", mkstream=True)
            except redis.exceptions.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        except Exception:
            raise CoordinationError("无法连接或初始化Redis；请确认服务和权限，不会自动降级为本地模式。") from None

    def _name(self, kind, key):
        return self._namespace + ":" + kind + ":" + _key(key)

    def get_cache(self, key):
        name = self._name("cache", key)
        try:
            raw = self._client.get(name)
            result = json.loads(_decode(raw)) if raw is not None else None
            if result is not None and not isinstance(result, dict):
                raise ValueError
            with self._metrics_lock:
                if result is None:
                    self._misses += 1
                else:
                    self._hits += 1
            return result
        except Exception:
            raise CoordinationError("Redis缓存读取失败。") from None

    def set_cache(self, key, value, ttl=300):
        name, milliseconds, raw = self._name("cache", key), max(1, math.ceil(_ttl(ttl) * 1000)), _cache_json(value)
        try:
            self._client.set(name, raw, px=milliseconds)
        except Exception:
            raise CoordinationError("Redis缓存写入失败。") from None

    def acquire_lock(self, key, ttl=900):
        name, milliseconds, token = self._name("lock", key), max(1, math.ceil(_ttl(ttl) * 1000)), uuid4().hex
        try:
            return token if self._client.set(name, token, nx=True, px=milliseconds) else None
        except Exception:
            raise CoordinationError("Redis锁获取失败。") from None

    def release_lock(self, key, token):
        name = self._name("lock", key)
        if not isinstance(token, str) or not token:
            return False
        try:
            for _ in range(4):
                try:
                    with self._client.pipeline() as pipe:
                        pipe.watch(name)
                        if _decode(pipe.get(name)) != token:
                            pipe.unwatch()
                            return False
                        pipe.multi()
                        pipe.delete(name)
                        pipe.execute()
                        return True
                except self._redis.exceptions.WatchError:
                    continue
            return False
        except Exception:
            raise CoordinationError("Redis锁释放失败。") from None

    def enqueue(self, change_id):
        change_id = _change(change_id)
        try:
            return _decode(self._client.xadd(self._stream, {"change_id": change_id}))
        except Exception:
            raise CoordinationError("Redis队列写入失败。") from None

    def claim(self, consumer, wait_ms=0):
        _change(consumer)
        if type(wait_ms) is not int or not 0 <= wait_ms <= 60000:
            raise ValueError("等待时间必须为0至60000毫秒。")
        try:
            # Continue scanning the PEL across calls. Starting at 0 every time
            # could starve stale pending items beyond the first scan page.
            with self._claim_lock:
                recovered = self._client.xautoclaim(self._stream, self._group, consumer,
                    min_idle_time=self._pending_idle_ms, start_id=self._claim_cursor, count=1)
                self._claim_cursor = _decode(recovered[0])
            messages = recovered[1]
            if not messages:
                kwargs = {"count": 1}
                if wait_ms:
                    kwargs["block"] = wait_ms
                streams = self._client.xreadgroup(self._group, consumer, {self._stream: ">"}, **kwargs)
                messages = streams[0][1] if streams else []
            if not messages:
                return None
            job_id, fields = messages[0]
            fields = {_decode(key): _decode(value) for key, value in fields.items()}
            return {"job_id": _decode(job_id), "change_id": _change(fields.get("change_id"))}
        except Exception:
            raise CoordinationError("Redis队列领取失败；任务仍应由持久评审ID防止重复处理。") from None

    def ack(self, job_id):
        try:
            return bool(self._client.xack(self._stream, self._group, job_id))
        except Exception:
            raise CoordinationError("Redis队列确认失败。") from None

    def stats(self):
        try:
            pending = self._client.xpending(self._stream, self._group)
            with self._metrics_lock:
                return {"backend": "redis", "durability": "redis_service_configuration",
                        "cache_hits": self._hits, "cache_misses": self._misses,
                        "queue_total": self._client.xlen(self._stream), "queue_pending": pending["pending"]}
        except Exception:
            raise CoordinationError("Redis统计读取失败。") from None

    def close(self):
        if self._owned_client:
            try:
                self._client.close()
            except Exception:
                raise CoordinationError("Redis连接关闭失败。") from None
