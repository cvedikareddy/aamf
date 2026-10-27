"""Target workload deployed to AWS Lambda (one copy per memory tier).

CPU part scales with Lambda memory (CPU is allocated proportionally to memory);
the sleep part stands in for a downstream call that no amount of memory speeds up.
That mix is what makes "bigger is better" false and gives the memory something to learn.
"""
import time


def handler(event, context):
    work = max(1, min(int(event.get("work", 1)), 10))
    extra_ms = max(0, min(int(event.get("extra_ms", 0)), 5000))
    features = event.get("context") or {}
    payload_kb = max(0.0, min(float(features.get("payload_size_kb", 0)), 4096.0))
    queue_depth = max(0.0, min(float(features.get("queue_depth", 0)), 1000.0))
    request_type = str(features.get("request_type", "read")).lower()
    payload_factor = 1.0 + min(payload_kb / 1024.0, 4.0) * 0.25
    type_factor = 1.2 if request_type in ("write", "update", "delete", "batch") else 1.0
    iterations = min(int(work * 600_000 * payload_factor * type_factor), 9_000_000)

    x = 0
    for i in range(iterations):
        x = (x * 31 + i) % 1_000_003

    queue_wait_s = min(queue_depth * 0.002, 0.3)
    time.sleep(0.15 + queue_wait_s + extra_ms / 1000.0)
    return {"statusCode": 200, "work": work, "payload_kb": payload_kb,
            "queue_depth": queue_depth, "request_type": request_type, "checksum": x}
