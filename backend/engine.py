"""Explainable, context-aware decision engines for AAMF and comparison baselines."""
import hashlib
import math
import random
import time

import config as C


def default_context(work: int) -> dict:
    """Build a reproducible feature vector when a caller supplies only work level."""
    from datetime import datetime, timezone
    w = max(C.WORK_MIN, min(int(work), C.WORK_MAX))
    return {
        "payload_size_kb": round(w * 64.0, 2),
        "hour_utc": datetime.now(timezone.utc).hour,
        "request_type": "write" if w % 2 == 0 else "read",
        "queue_depth": (w - 1) * 2,
        "cold_start_rate": 0.05,
    }


def normalize_context(context, work: int) -> dict:
    base = default_context(work)
    if isinstance(context, dict):
        base.update({k: context[k] for k in C.CONTEXT_FEATURES if k in context and context[k] is not None})
    try:
        base["payload_size_kb"] = max(0.0, min(float(base["payload_size_kb"]), 1048576.0))
        base["hour_utc"] = max(0.0, min(float(base["hour_utc"]), 23.999))
        base["queue_depth"] = max(0.0, min(float(base["queue_depth"]), 1000000.0))
        base["cold_start_rate"] = max(0.0, min(float(base["cold_start_rate"]), 1.0))
        base["request_type"] = str(base["request_type"]).strip().lower()[:48] or "other"
    except (TypeError, ValueError) as exc:
        raise ValueError("Workload features must be numeric except request_type.") from exc
    return base


def context_cluster_key(context, work: int) -> int:
    """Stable table-compatible key for a coarse workload feature cluster."""
    features = normalize_context(context, work)
    payload_bin = min(24, max(0, int(math.log2(float(features["payload_size_kb"]) + 1) * 2)))
    hour_bin = int(float(features["hour_utc"]) // 2)
    queue = float(features["queue_depth"])
    queue_bin = 0 if queue == 0 else 1 if queue <= 5 else 2 if queue <= 20 else 3 if queue <= 100 else 4
    cold = float(features["cold_start_rate"])
    cold_bin = 0 if cold == 0 else 1 if cold <= .05 else 2 if cold <= .2 else 3 if cold <= .5 else 4
    request_type = str(features["request_type"])[:48]
    bucket = f"{int(work)}|p{payload_bin}|h{hour_bin}|t{request_type}|q{queue_bin}|c{cold_bin}"
    digest = hashlib.sha256(bucket.encode("utf-8")).hexdigest()
    return 1000 + int(digest[:12], 16)


def context_similarity(left, right) -> float:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return 0.0
    distance, total_weight = 0.0, 0.0
    for key in C.CONTEXT_FEATURES:
        weight = C.FEATURE_WEIGHTS[key]
        scale = C.FEATURE_SCALES.get(key, 1.0)
        try:
            if key == "request_type":
                delta = 0.0 if str(left.get(key, "other")).lower() == str(right.get(key, "other")).lower() else 1.0
            elif key == "hour_utc":
                diff = abs(float(left[key]) - float(right[key]))
                delta = min(diff, 24.0 - min(diff, 24.0)) / scale
            else:
                delta = min(abs(float(left[key]) - float(right[key])) / scale, 1.0)
        except (KeyError, TypeError, ValueError):
            delta = 1.0
        distance += weight * delta
        total_weight += weight
    return math.exp(-distance / max(total_weight * C.CONTEXT_SIMILARITY_TAU, 1e-9))


def loss_of(latency_ms: float, cost_ud: float, cost_weight: float | None = None) -> float:
    cw = C.COST_W if cost_weight is None else max(0.0, min(float(cost_weight), 1.0))
    latency_weight = 1.0 - cw
    loss = latency_weight * latency_ms / C.SLO_MS + cw * cost_ud / C.COST_REF_UD
    if latency_ms > C.SLO_MS:
        loss += C.VIOLATION_PENALTY
    return loss


def _solve(matrix, vector):
    """Small Gauss-Jordan solver for the counterfactual weighted least-squares fit."""
    n = len(vector)
    a = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-10:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            a[row] = [v - factor * p for v, p in zip(a[row], a[col])]
    return [a[i][-1] for i in range(n)]


def _model_features(context, tier, extra_ms, work):
    payload = float(context.get("payload_size_kb", 0)) / 1024.0
    queue = float(context.get("queue_depth", 0)) / 50.0
    write = 1.0 if str(context.get("request_type", "")).lower() in ("write", "update", "delete", "mutation") else 0.0
    return [1.0, float(work), payload, queue, max(0.0, float(extra_ms)) / 1000.0,
            float(work) * 1024.0 / float(tier), write]


def _fit_counterfactual(episodes, context, extra_ms):
    observations = []
    for episode in episodes:
        features = episode.get("context_features")
        if not features or episode.get("policy") != "aamf" or not episode.get("latency_ms"):
            continue
        similarity = context_similarity(context, features)
        if similarity < 0.08:
            continue
        tier = episode.get("config")
        if tier not in C.TIERS:
            continue
        try:
            x = _model_features(features, tier, episode.get("extra_ms", 0), episode.get("ctx", 1))
            y = float(episode["latency_ms"])
            observations.append((x, y, similarity))
        except (TypeError, ValueError):
            continue
    if len(observations) < C.COUNTERFACTUAL_MIN_EPISODES or len({e.get("config") for e in episodes if e.get("policy") == "aamf"}) < 2:
        return None
    width = len(observations[0][0])
    normal = [[0.0] * width for _ in range(width)]
    target = [0.0] * width
    for x, y, weight in observations:
        for i in range(width):
            target[i] += weight * x[i] * y
            for j in range(width):
                normal[i][j] += weight * x[i] * x[j]
    for i in range(1, width):
        normal[i][i] += 1e-5
    coef = _solve(normal, target)
    if not coef:
        return None
    predictions = {}
    for tier in C.TIERS:
        estimate = sum(a * b for a, b in zip(coef, _model_features(context, tier, extra_ms, context.get("work_level", 1))))
        predictions[tier] = max(1.0, min(estimate, C.SLO_MS * 10))
    return {"latency_ms": predictions, "samples": len(observations), "model": "weighted CPU-time + wait regression"}


def _frontier_loss(row, cost_weight):
    points = row.get("frontier") or []
    if points:
        return min(loss_of(float(p["latency_ms"]), float(p["cost_ud"]), cost_weight) for p in points)
    return loss_of(float(row.get("ema_latency", 0)), float(row.get("ema_cost", 0)), cost_weight)


def _ci(samples, cost_weight):
    values = [loss_of(float(s["latency_ms"]), float(s["cost_ud"]), cost_weight) for s in samples]
    if not values:
        return None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return {"low": round(mean, 3), "high": round(mean, 3), "n": len(values)}
    var = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    margin = 1.96 * math.sqrt(var / len(values))
    return {"low": round(max(0.0, mean - margin), 3), "high": round(mean + margin, 3), "n": len(values)}


class AAMF:
    name = "aamf"

    def __init__(self, memory):
        self.mem = memory

    def decide(self, ctx: int, context=None, cost_weight=None, extra_ms=0):
        features = normalize_context(context, ctx)
        features["work_level"] = int(ctx)
        cw = C.COST_W if cost_weight is None else max(0.0, min(float(cost_weight), 1.0))
        cluster = context_cluster_key(features, ctx)
        rows = self.mem.neighbors(ctx, C.RADIUS)
        episodes = self.mem.recent(C.COUNTERFACTUAL_MAX_EPISODES)
        counterfactual = _fit_counterfactual(episodes, features, extra_ms)
        candidates, evidence = [], []
        for row in rows:
            sim = context_similarity(features, row.get("context_features") or default_context(row.get("work_level", row["ctx"])))
            if sim >= 0.05 and row.get("n", 0) > 0:
                evidence.append({"work": int(row.get("work_level", row["ctx"])), "tier": row["config"],
                                "context_features": row.get("context_features") or default_context(row.get("work_level", row["ctx"])),
                                "similarity": round(sim, 3), "weight": round(sim * row["n"], 3), "samples": row["n"]})
        for tier in C.TIERS:
            num = den = 0.0
            direct = 0
            rows_for_tier = []
            for row in rows:
                if row["config"] != tier or row.get("n", 0) <= 0:
                    continue
                similarity = context_similarity(features, row.get("context_features") or default_context(row.get("work_level", row["ctx"])))
                weight = row["n"] * similarity
                if weight <= 0.01:
                    continue
                row_loss = _frontier_loss(row, cw)
                num += weight * -row_loss
                den += weight
                rows_for_tier.append(row)
                if int(row["ctx"]) == cluster and similarity > 0.85:
                    direct += row["n"]
            memory_mean = num / den if den else None
            predicted_loss = None
            if counterfactual:
                predicted_loss = loss_of(counterfactual["latency_ms"][tier],
                                         (tier / 1024.0) * (counterfactual["latency_ms"][tier] / 1000.0) * C.PRICE_GB_S * 1e6 + C.PRICE_REQ * 1e6,
                                         cw)
            if memory_mean is None:
                mean = -predicted_loss if predicted_loss is not None else 0.0
            elif predicted_loss is not None:
                prior_weight = min(0.25, 1.0 / (4.0 + den))
                mean = memory_mean * (1.0 - prior_weight) - predicted_loss * prior_weight
            else:
                mean = memory_mean
            candidates.append({"tier": tier, "mean": mean, "conf": den, "direct": direct,
                               "score": mean + C.UCB_C / math.sqrt(den + 1.0),
                               "predicted_loss": predicted_loss,
                               "ci": _ci([sample for row in rows_for_tier for sample in row.get("samples", [])], cw)})

        broke = [(int(r.get("work_level", r["ctx"])), r["config"]) for r in rows
                 if r.get("n", 0) > 0 and r.get("ema_latency", 0) > C.SLO_MS]
        for candidate in candidates:
            candidate["pruned"] = candidate["direct"] == 0 and any(
                candidate["tier"] <= failed_tier and ctx >= failed_ctx for failed_ctx, failed_tier in broke)
        pruned = sorted(c["tier"] for c in candidates if c["pruned"])
        tiers = C.TIERS
        known = [c for c in candidates if c["conf"] > 0]
        anchor = tiers.index(max(known, key=lambda c: c["mean"])["tier"]) if known else tiers.index(C.STATIC_TIER)
        for candidate in candidates:
            candidate["far"] = candidate["conf"] == 0 and abs(tiers.index(candidate["tier"]) - anchor) > 1
        shortlist = [c for c in candidates if not c["pruned"] and not c["far"]] or candidates
        random.shuffle(shortlist)
        best = max(shortlist, key=lambda c: c["score"])
        known = [c for c in shortlist if c["conf"] > 0]
        greedy = max(known, key=lambda c: c["mean"]) if known else None
        if best["conf"] == 0:
            source, why = "explore", f"{best['tier']} MB has no similar-context samples; counterfactual prior used when available"
        elif greedy is not None and best is not greedy:
            source, why = "explore", "uncertainty bonus outweighs the best remembered Pareto point"
        elif best["direct"] == 0:
            source, why = "transfer", "nearest workload contexts supplied the Pareto evidence"
        else:
            source, why = "exploit", "best remembered point on this tier's latency-cost frontier"
        top = sorted(shortlist, key=lambda c: -c["score"])[:3]
        return best["tier"], {
            "source": source, "reason": why, "pruned": pruned, "context_features": features,
            "objective": {"latency_weight": round(1.0 - cw, 3), "cost_weight": round(cw, 3)},
            "counterfactual": counterfactual,
            "neighbors": sorted(evidence, key=lambda e: -e["weight"])[:12],
            "candidates": [{"tier": c["tier"], "remembered_loss": round(-c["mean"], 3),
                            "confidence": round(c["conf"], 2), "predicted_loss": round(c["predicted_loss"], 3) if c["predicted_loss"] is not None else None,
                            "ci": c["ci"]} for c in top],
        }

    def learn(self, ctx: int, tier: int, res: dict, context=None, cost_weight=None) -> dict:
        features = normalize_context(context, ctx)
        features["work_level"] = int(ctx)
        cw = C.COST_W if cost_weight is None else max(0.0, min(float(cost_weight), 1.0))
        objective_loss = loss_of(res["latency_ms"], res["cost_ud"], cw)
        cluster = context_cluster_key(features, ctx)
        row = self.mem.get(cluster, tier) or {
            "ctx": int(cluster), "work_level": int(ctx), "config": int(tier), "n": 0, "ema_reward": 0.0,
            "ema_latency": 0.0, "ema_cost": 0.0, "ema_loss": 0.0,
            "frontier": [], "samples": [], "drift_mean": 0.0, "drift_cum": 0.0,
            "drift_n": 0, "drift_events": 0,
        }
        old_n = int(row.get("n", 0))
        detector_n = int(row.get("drift_n", 0))
        detector_mean = float(row.get("drift_mean", 0.0))
        detector_cum = max(0.0, float(row.get("drift_cum", 0.0)) + objective_loss - detector_mean - C.DRIFT_DELTA)
        detector_mean = objective_loss if detector_n == 0 else detector_mean + (objective_loss - detector_mean) / (detector_n + 1)
        detector_n += 1
        drift = detector_n >= C.DRIFT_MIN_SAMPLES and detector_cum > C.DRIFT_THRESHOLD
        prior_mean = float(row.get("ema_loss", objective_loss))
        if drift:
            row["n"] = 0
            row["ema_reward"] = row["ema_latency"] = row["ema_cost"] = row["ema_loss"] = 0.0
            row["frontier"], row["samples"] = [], []
            row["drift_events"] = int(row.get("drift_events", 0)) + 1
            old_n = 0
            detector_cum, detector_n = 0.0, 0
        alpha = 1.0 if old_n == 0 else max(C.ALPHA, 1.0 / (old_n + 1))
        row["ema_reward"] = float(row.get("ema_reward", 0.0)) + alpha * (-objective_loss - float(row.get("ema_reward", 0.0)))
        row["ema_latency"] = float(row.get("ema_latency", 0.0)) + alpha * (float(res["latency_ms"]) - float(row.get("ema_latency", 0.0)))
        row["ema_cost"] = float(row.get("ema_cost", 0.0)) + alpha * (float(res["cost_ud"]) - float(row.get("ema_cost", 0.0)))
        row["ema_loss"] = prior_mean + alpha * (objective_loss - prior_mean)
        row["n"] = min(old_n + 1, C.N_CAP)
        row["work_level"] = int(ctx)
        row["context_features"] = {k: v for k, v in features.items() if k in C.CONTEXT_FEATURES}
        sample = {"latency_ms": float(res["latency_ms"]), "cost_ud": float(res["cost_ud"]),
                  "loss": objective_loss, "at": int(time.time())}
        row["samples"] = (row.get("samples", []) + [sample])[-C.SAMPLE_CAP:]
        points = row.get("frontier", []) + [{"latency_ms": float(res["latency_ms"]), "cost_ud": float(res["cost_ud"])}]
        frontier = []
        for point in points:
            dominated = any(other is not point and float(other["latency_ms"]) <= float(point["latency_ms"])
                            and float(other["cost_ud"]) <= float(point["cost_ud"])
                            and (float(other["latency_ms"]) < float(point["latency_ms"])
                                 or float(other["cost_ud"]) < float(point["cost_ud"])) for other in points)
            if not dominated and point not in frontier:
                frontier.append(point)
        if len(frontier) > C.FRONTIER_CAP:
            frontier = sorted(frontier, key=lambda p: (float(p["latency_ms"]), float(p["cost_ud"])))[:C.FRONTIER_CAP]
        row["frontier"] = frontier
        row["drift_mean"], row["drift_cum"], row["drift_n"] = detector_mean, detector_cum, detector_n
        row["updated"] = int(time.time())
        self.mem.put(row)
        return {"forgot": drift, "drift_detected": drift, "drift_score": round(detector_cum, 4),
                "drift_reason": "Page-Hinkley detected a sustained loss increase for this context/tier" if drift else None,
                "objective_loss": round(objective_loss, 4), "confidence_interval": _ci(row["samples"], cw)}


class StaticTier:
    name = "static"
    def __init__(self, tier=C.STATIC_TIER):
        self.tier = tier
    def decide(self, ctx, **kwargs):
        return self.tier, {"source": "static", "reason": "fixed by operator"}
    def learn(self, ctx, tier, res, **kwargs):
        return {}


class ThresholdRule:
    """Reactive comparison baseline that scales on SLO pressure."""
    name = "threshold"
    def __init__(self):
        self.i, self.calm = 0, 0
    def decide(self, ctx, **kwargs):
        return C.TIERS[self.i], {"source": "rule", "reason": "current step of reactive rule"}
    def learn(self, ctx, tier, res, **kwargs):
        lat = res["latency_ms"]
        if lat > 0.8 * C.SLO_MS:
            self.i, self.calm = min(self.i + 1, len(C.TIERS) - 1), 0
        elif lat < 0.25 * C.SLO_MS:
            self.calm += 1
            if self.calm >= 3:
                self.i, self.calm = max(self.i - 1, 0), 0
        else:
            self.calm = 0
        return {}
