"""Memory layer.

Two stores, same interface:
  DynamoMemory : the real thing (DynamoDB, persists across restarts and sessions)
  LocalMemory  : in-process dict used by "demo mode" so the site works with no AWS account

Two kinds of memory are kept:
  policy   (ctx, config) -> consolidated stats  {n, ema_reward, ema_latency, ema_cost}
  episodes raw log of every decision + outcome (the audit trail / lineage)
"""
import threading
import time
import uuid
from decimal import Decimal

import config as C

try:
    from boto3.dynamodb.conditions import Key
except ImportError:  # demo mode works without boto3 installed
    Key = None

_INT_FIELDS = ("ctx", "config", "n", "expires", "updated")


def _to_dec(d: dict) -> dict:
    return {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in d.items()}


def _from_dec(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if k in _INT_FIELDS else float(v)
        else:
            out[k] = v
    return out


def _new_ts() -> str:
    # sortable and collision-proof even when 3 policies write in the same millisecond
    return f"{int(time.time() * 1000):013d}#{uuid.uuid4().hex[:6]}"


class DynamoMemory:
    kind = "dynamodb"

    def __init__(self, boto_session):
        ddb = boto_session.resource("dynamodb")
        self.policy = ddb.Table(C.TABLE_POLICY)
        self.episodes = ddb.Table(C.TABLE_EPISODES)

    # ---- consolidated policy memory -------------------------------------------------
    def neighbors(self, ctx: int, radius: int) -> list:
        rows = []
        for c in range(max(C.WORK_MIN, ctx - radius), min(C.WORK_MAX, ctx + radius) + 1):
            resp = self.policy.query(KeyConditionExpression=Key("ctx").eq(c))
            rows += [_from_dec(i) for i in resp["Items"]]
        return rows

    def get(self, ctx: int, config: int):
        item = self.policy.get_item(Key={"ctx": ctx, "config": config}).get("Item")
        return _from_dec(item) if item else None

    def put(self, row: dict) -> None:
        self.policy.put_item(Item=_to_dec(row))

    def snapshot(self) -> list:
        rows, kw = [], {}
        while True:
            r = self.policy.scan(**kw)
            rows += [_from_dec(i) for i in r["Items"]]
            if "LastEvaluatedKey" not in r:
                return rows
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    # ---- episodic log ---------------------------------------------------------------
    def log_episode(self, ep: dict) -> None:
        item = dict(ep, stream="global", ts=_new_ts(),
                    expires=int(time.time()) + C.EPISODE_TTL_S)
        self.episodes.put_item(Item=_to_dec(item))

    def recent(self, limit: int = 20) -> list:
        r = self.episodes.query(KeyConditionExpression=Key("stream").eq("global"),
                                ScanIndexForward=False, Limit=limit)
        return [_from_dec(i) for i in r["Items"]]

    # ---- admin ----------------------------------------------------------------------
    def reset(self) -> None:
        for table, keys in ((self.policy, ("ctx", "config")), (self.episodes, ("stream", "ts"))):
            kw = {}
            while True:
                r = table.scan(ProjectionExpression="#a, #b",
                               ExpressionAttributeNames={"#a": keys[0], "#b": keys[1]}, **kw)
                with table.batch_writer() as bw:
                    for it in r["Items"]:
                        bw.delete_item(Key={keys[0]: it[keys[0]], keys[1]: it[keys[1]]})
                if "LastEvaluatedKey" not in r:
                    break
                kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


class LocalMemory:
    kind = "in-process (demo)"

    def __init__(self):
        self._policy, self._episodes = {}, []
        self._lock = threading.Lock()

    def neighbors(self, ctx, radius):
        with self._lock:
            return [dict(r) for (c, _), r in self._policy.items() if abs(c - ctx) <= radius]

    def get(self, ctx, config):
        with self._lock:
            r = self._policy.get((ctx, config))
            return dict(r) if r else None

    def put(self, row):
        with self._lock:
            self._policy[(row["ctx"], row["config"])] = dict(row)

    def snapshot(self):
        with self._lock:
            return [dict(r) for r in self._policy.values()]

    def log_episode(self, ep):
        with self._lock:
            self._episodes.append(dict(ep, stream="global", ts=_new_ts()))

    def recent(self, limit=20):
        with self._lock:
            return list(reversed(self._episodes[-limit:]))

    def reset(self):
        with self._lock:
            self._policy.clear()
            self._episodes.clear()
