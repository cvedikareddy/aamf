"""Decision engines.

AAMF (the contribution):
  decide(): recall memory for this context AND its neighbours (distance-weighted),
            score every architecture tier = remembered reward + uncertainty bonus.
  learn() : consolidate the outcome into memory (EMA). If the outcome is far from what
            memory predicted ("surprise"), confidence in that memory is cut so the system
            re-learns quickly instead of trusting a stale belief  -> surprise-gated forgetting.

Baselines (what most cloud setups do today):
  StaticTier    : fixed size picked by a human.
  ThresholdRule : reactive CloudWatch-alarm style step scaling. Stateless w.r.t. context,
                  remembers nothing between requests except its current setting.
"""
import math
import random
import time

import config as C


def loss_of(latency_ms: float, cost_ud: float) -> float:
    loss = C.LAT_W * latency_ms / C.SLO_MS + C.COST_W * cost_ud / C.COST_REF_UD
    if latency_ms > C.SLO_MS:
        loss += C.VIOLATION_PENALTY
    return loss


class AAMF:
    name = "aamf"

    def __init__(self, memory):
        self.mem = memory

    def decide(self, ctx: int):
        rows = self.mem.neighbors(ctx, C.RADIUS)
        cands = []
        for tier in C.TIERS:
            num = den = 0.0
            direct = 0
            for r in rows:
                if r["config"] != tier or r["n"] <= 0:
                    continue
                w = r["n"] * math.exp(-abs(r["ctx"] - ctx) / C.TAU)
                num += w * r["ema_reward"]
                den += w
                if r["ctx"] == ctx:
                    direct = r["n"]
            mean = num / den if den else 0.0          # unseen = 0 = best possible reward (optimism)
            cands.append({"tier": tier, "mean": mean, "conf": den, "direct": direct,
                          "score": mean + C.UCB_C / math.sqrt(den + 1.0)})

        # Dominance pruning (architectural prior): if tier T broke the SLO at context C, then any
        # smaller tier at an equal-or-heavier context will too. Skip them instead of paying to try.
        broke = [(r["ctx"], r["config"]) for r in rows if r["n"] > 0 and r["ema_latency"] > C.SLO_MS]
        for c in cands:
            c["pruned"] = c["direct"] == 0 and any(c["tier"] <= bt and ctx >= bc for bc, bt in broke)
        pruned = sorted(c["tier"] for c in cands if c["pruned"])

        # Unimodality prior: loss is roughly U-shaped over ordered tiers, so only try an unseen tier
        # when it sits next to the best remembered one (hill-climb). Cold start begins mid-range.
        tiers = C.TIERS
        known0 = [c for c in cands if c["conf"] > 0]
        anchor = (tiers.index(max(known0, key=lambda c: c["mean"])["tier"]) if known0
                  else tiers.index(C.STATIC_TIER))
        for c in cands:
            c["far"] = c["conf"] == 0 and abs(tiers.index(c["tier"]) - anchor) > 1

        cands = [c for c in cands if not c["pruned"] and not c["far"]] or cands

        random.shuffle(cands)                          # random tie-break among untried tiers
        best = max(cands, key=lambda c: c["score"])
        known = [c for c in cands if c["conf"] > 0]
        greedy = max(known, key=lambda c: c["mean"]) if known else None

        if best["conf"] == 0:
            source, why = "explore", f"{best['tier']} MB never tried near context {ctx}"
        elif greedy is not None and best is not greedy:
            source, why = "explore", "uncertainty bonus outweighs best known option"
        elif best["direct"] == 0:
            source, why = "transfer", f"no history at context {ctx}; inferred from neighbouring contexts"
        else:
            source, why = "exploit", f"best remembered outcome at context {ctx}"

        top = sorted(cands, key=lambda c: -c["score"])[:3]
        return best["tier"], {
            "source": source, "reason": why, "pruned": pruned,
            "candidates": [{"tier": c["tier"], "remembered_loss": round(-c["mean"], 3),
                            "confidence": round(c["conf"], 2)} for c in top],
        }

    def learn(self, ctx: int, tier: int, res: dict) -> dict:
        loss = loss_of(res["latency_ms"], res["cost_ud"])
        reward = -loss
        row = self.mem.get(ctx, tier) or {
            "ctx": ctx, "config": tier, "n": 0,
            "ema_reward": 0.0, "ema_latency": 0.0, "ema_cost": 0.0}

        surprise, forgot = 0.0, False
        if row["n"] >= 3:
            surprise = abs(reward - row["ema_reward"])
            if surprise > C.SURPRISE_T:                # the world changed: distrust old memory
                row["n"], forgot = C.FORGET_N, True

        n = row["n"]
        a = 1.0 if n == 0 else max(C.ALPHA, 1.0 / (n + 1))
        row["ema_reward"] += a * (reward - row["ema_reward"])
        row["ema_latency"] += a * (res["latency_ms"] - row["ema_latency"])
        row["ema_cost"] += a * (res["cost_ud"] - row["ema_cost"])
        row["n"] = min(n + 1, C.N_CAP)
        row["updated"] = int(time.time())
        self.mem.put(row)
        return {"surprise": round(surprise, 3), "forgot": forgot}


class StaticTier:
    name = "static"

    def __init__(self, tier=C.STATIC_TIER):
        self.tier = tier

    def decide(self, ctx):
        return self.tier, {"source": "static", "reason": "fixed by operator"}

    def learn(self, ctx, tier, res):
        return {}


class ThresholdRule:
    """Scale up one step on an SLO-pressure breach; scale down after 3 calm readings."""
    name = "threshold"

    def __init__(self):
        self.i, self.calm = 0, 0

    def decide(self, ctx):
        return C.TIERS[self.i], {"source": "rule", "reason": "current step of reactive rule"}

    def learn(self, ctx, tier, res):
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
