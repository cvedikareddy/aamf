"""Persistent and in-process memory stores for AAMF decisions and audit episodes."""
import copy
import threading
import time
import uuid
from decimal import Decimal

import config as C

try:
    from boto3.dynamodb.conditions import Key
except ImportError:  # Demo mode remains usable without boto3.
    Key = None

_INT_FIELDS = {"ctx", "config", "n", "expires", "updated"}


def _to_native(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    if isinstance(value, tuple):
        return [_to_native(v) for v in value]
    return value


def _from_native(value, field=None):
    if isinstance(value, Decimal):
        return int(value) if field in _INT_FIELDS else float(value)
    if isinstance(value, dict):
        return {k: _from_native(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_native(v) for v in value]
    return value


def _new_ts():
    return f"{int(time.time() * 1000):013d}#{uuid.uuid4().hex[:8]}"


class DynamoMemory:
    """DynamoDB implementation; uses the existing (ctx, config) table keys."""
    kind = "dynamodb"

    def __init__(self, boto_session):
        ddb = boto_session.resource("dynamodb")
        self.policy = ddb.Table(C.TABLE_POLICY)
        self.episodes = ddb.Table(C.TABLE_EPISODES)
        self._policy_cache = []
        self._cache_at = 0.0
        self._cache_lock = threading.Lock()

    def neighbors(self, ctx, radius):
        return [row for row in self.snapshot() if abs(int(row.get("work_level", row["ctx"])) - int(ctx)) <= radius]

    def get(self, ctx, config):
        item = self.policy.get_item(Key={"ctx": int(ctx), "config": int(config)}).get("Item")
        return _from_native(item) if item else None

    def put(self, row):
        item = copy.deepcopy(row)
        self.policy.put_item(Item=_to_native(item))
        with self._cache_lock:
            self._policy_cache = [existing for existing in self._policy_cache
                                  if not (int(existing["ctx"]) == int(item["ctx"])
                                          and int(existing["config"]) == int(item["config"]))]
            self._policy_cache.append(item)

    def snapshot(self):
        now = time.time()
        with self._cache_lock:
            if self._cache_at and now - self._cache_at < 60:
                return copy.deepcopy(self._policy_cache)
            rows = []
            paginator = self.policy.meta.client.get_paginator("scan")
            for page in paginator.paginate(TableName=self.policy.name):
                rows.extend(_from_native(item) for item in page.get("Items", []))
            self._policy_cache, self._cache_at = rows, now
            return copy.deepcopy(rows)

    def log_episode(self, ep):
        item = dict(ep, stream="global", ts=_new_ts(), expires=int(time.time()) + C.EPISODE_TTL_S)
        self.episodes.put_item(Item=_to_native(item))

    def recent(self, limit=20):
        r = self.episodes.query(KeyConditionExpression=Key("stream").eq("global"),
                                ScanIndexForward=False, Limit=limit)
        return [_from_native(i) for i in r.get("Items", [])]

    def reset(self):
        for table, keys in ((self.policy, ("ctx", "config")), (self.episodes, ("stream", "ts"))):
            keys_to_delete = []
            paginator = table.meta.client.get_paginator("scan")
            projection = "#a, #b"
            names = {"#a": keys[0], "#b": keys[1]}
            for page in paginator.paginate(TableName=table.name, ProjectionExpression=projection,
                                           ExpressionAttributeNames=names):
                keys_to_delete.extend({keys[0]: item[keys[0]], keys[1]: item[keys[1]]}
                                      for item in page.get("Items", []))
            with table.batch_writer() as batch:
                for key in keys_to_delete:
                    batch.delete_item(Key=key)
        with self._cache_lock:
            self._policy_cache, self._cache_at = [], time.time()


class LocalMemory:
    """Thread-safe in-process equivalent for demo sessions."""
    kind = "in-process (demo)"

    def __init__(self):
        self._policy, self._episodes = {}, []
        self._lock = threading.Lock()

    def neighbors(self, ctx, radius):
        with self._lock:
            return [copy.deepcopy(r) for r in self._policy.values()
                    if abs(int(r.get("work_level", r["ctx"])) - int(ctx)) <= radius]

    def get(self, ctx, config):
        with self._lock:
            row = self._policy.get((int(ctx), int(config)))
            return copy.deepcopy(row) if row else None

    def put(self, row):
        with self._lock:
            self._policy[(int(row["ctx"]), int(row["config"]))] = copy.deepcopy(row)

    def snapshot(self):
        with self._lock:
            return [copy.deepcopy(r) for r in self._policy.values()]

    def log_episode(self, ep):
        with self._lock:
            self._episodes.append(copy.deepcopy(dict(ep, stream="global", ts=_new_ts())))

    def recent(self, limit=20):
        with self._lock:
            return copy.deepcopy(list(reversed(self._episodes[-limit:])))

    def reset(self):
        with self._lock:
            self._policy.clear()
            self._episodes.clear()
