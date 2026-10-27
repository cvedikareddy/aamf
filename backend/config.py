"""Single source of truth for AAMF names, workload features, and learning knobs."""
import os

PREFIX = "aamf"
DEFAULT_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
TABLE_POLICY = f"{PREFIX}-policy"
TABLE_EPISODES = f"{PREFIX}-episodes"
ROLE_NAME = f"{PREFIX}-lambda-role"
EPISODE_TTL_S = 7 * 24 * 3600

TIERS = [128, 256, 512, 1024, 2048]
STATIC_TIER = 512


def fn_name(tier: int) -> str:
    return f"{PREFIX}-target-{tier}"

# Work remains a coarse fallback key for the existing DynamoDB table schema;
# decisions use the richer feature vector and its learned similarity.
WORK_MIN, WORK_MAX = 1, 6
CONTEXT_FEATURES = ("payload_size_kb", "hour_utc", "request_type", "queue_depth", "cold_start_rate")
FEATURE_SCALES = {"payload_size_kb": 1024.0, "hour_utc": 12.0, "queue_depth": 50.0, "cold_start_rate": 0.5}
FEATURE_WEIGHTS = {"payload_size_kb": 1.0, "hour_utc": 0.5, "request_type": 0.8, "queue_depth": 1.2, "cold_start_rate": 0.8}
CONTEXT_SIMILARITY_TAU = 0.42

SIM_CPU_MS = 110.0
SLO_MS = 1500.0
LAT_W, COST_W = 0.4, 0.6
COST_REF_UD = 6.0
VIOLATION_PENALTY = 1.0
PRICE_GB_S = 0.0000166667
PRICE_REQ = 0.0000002

ALPHA = 0.3
TAU = 1.0
RADIUS = 2
UCB_C = 0.6
N_CAP = 100
# Page-Hinkley detector catches persistent increases in normalized loss and
# resets only the affected context/tier cell.
DRIFT_DELTA = 0.015
DRIFT_THRESHOLD = 0.35
DRIFT_MIN_SAMPLES = 5
FORGET_N = 2
FRONTIER_CAP = 40
SAMPLE_CAP = 30
COUNTERFACTUAL_MAX_EPISODES = 250
COUNTERFACTUAL_MIN_EPISODES = 6
SESSION_TTL_S = 3600
