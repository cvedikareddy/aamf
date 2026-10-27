"""Target workload deployed to AWS Lambda (one copy per memory tier).

CPU part scales with Lambda memory (CPU is allocated proportionally to memory);
the sleep part stands in for a downstream call that no amount of memory speeds up.
That mix is what makes "bigger is better" false and gives the memory something to learn.
"""
import time


def handler(event, context):
    work = max(1, min(int(event.get("work", 1)), 10))
    extra_ms = max(0, min(int(event.get("extra_ms", 0)), 5000))  # injected downstream slowdown

    x = 0
    for i in range(work * 600_000):
        x = (x * 31 + i) % 1_000_003

    time.sleep(0.15 + extra_ms / 1000.0)
    return {"statusCode": 200, "work": work, "checksum": x}
