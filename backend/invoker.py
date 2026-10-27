"""Runs one request against a tier and returns measured latency + cost.

Real mode : lambda.invoke(LogType='Tail') and parse the REPORT line, so the numbers are
            AWS's own billed duration, not client-side stopwatch noise.
Demo mode : closed-form simulation of the same workload (no AWS needed).
"""
import base64
import json
import math
import random
import re

import config as C

_DUR = re.compile(r"(?<![A-Za-z] )Duration: ([\d.]+) ms")
_BILLED = re.compile(r"Billed Duration: (\d+) ms")
_INIT = re.compile(r"Init Duration: ([\d.]+) ms")


def _cost_ud(tier: int, billed_ms: float) -> float:
    usd = (tier / 1024.0) * (billed_ms / 1000.0) * C.PRICE_GB_S + C.PRICE_REQ
    return usd * 1e6


def invoke_real(lambda_client, tier: int, work: int, extra_ms: int) -> dict:
    resp = lambda_client.invoke(
        FunctionName=C.fn_name(tier),
        Payload=json.dumps({"work": work, "extra_ms": extra_ms}).encode(),
        LogType="Tail",
    )
    body = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda {C.fn_name(tier)} failed: {body[:300]!r}")

    log = base64.b64decode(resp["LogResult"]).decode("utf-8", "replace")
    report = next((l for l in log.splitlines() if l.startswith("REPORT")), "")
    dur, billed = _DUR.search(report), _BILLED.search(report)
    if not (dur and billed):
        raise RuntimeError("Could not parse Lambda REPORT line")
    init = _INIT.search(report)
    billed_ms = float(billed.group(1))
    return {
        "latency_ms": float(dur.group(1)),      # warm-equivalent: init time reported separately
        "billed_ms": billed_ms,
        "cost_ud": _cost_ud(tier, billed_ms),
        "cold_ms": float(init.group(1)) if init else 0.0,
    }


def invoke_sim(tier: int, work: int, extra_ms: int) -> dict:
    share = min(tier / 1769.0, 1.0)                 # Lambda gives ~1 vCPU at 1769 MB
    cpu_ms = C.SIM_CPU_MS * work / max(share, 0.05)
    dur = (cpu_ms + 150.0 + extra_ms) * random.lognormvariate(0, 0.05)
    billed = math.ceil(dur)
    cold = 180.0 if random.random() < 0.06 else 0.0
    return {"latency_ms": round(dur, 2), "billed_ms": float(billed),
            "cost_ud": _cost_ud(tier, billed), "cold_ms": cold}


def invoke(app_session, tier: int, work: int, extra_ms: int) -> dict:
    if app_session.demo:
        return invoke_sim(tier, work, extra_ms)
    return invoke_real(app_session.lambda_client, tier, work, extra_ms)
