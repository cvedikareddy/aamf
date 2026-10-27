"""Single source of truth for names, tiers and the reward function weights."""
import os

PREFIX = "aamf"
DEFAULT_REGION = os.getenv("AWS_REGION", "ap-southeast-2")

# AWS resource names
TABLE_POLICY = f"{PREFIX}-policy"        # consolidated memory: (ctx, config) -> stats
TABLE_EPISODES = f"{PREFIX}-episodes"    # raw experience log (TTL 7 days)
ROLE_NAME = f"{PREFIX}-lambda-role"
EPISODE_TTL_S = 7 * 24 * 3600

# The "architecture knob": one Lambda variant per memory tier.
# (In production you would call update_function_configuration instead; variants
#  keep the demo instant and deterministic.)
TIERS = [128, 256, 512, 1024, 2048]
STATIC_TIER = 512


def fn_name(tier: int) -> str:
    return f"{PREFIX}-target-{tier}"


# Workload context = "work level" (how heavy the request is). 1 = light, 6 = heavy.
WORK_MIN, WORK_MAX = 1, 6

# Simulator calibration (demo mode only): CPU ms per work unit on a full vCPU
SIM_CPU_MS = 110.0

# Reward: lower loss is better.  loss = 0.4*lat/SLO + 0.6*cost/COST_REF (+ penalty on SLO breach)
SLO_MS = 1500.0
LAT_W, COST_W = 0.4, 0.6
COST_REF_UD = 6.0            # micro-dollars per request treated as "1.0"
VIOLATION_PENALTY = 1.0

# Lambda public pricing (x86, us-east-1 style numbers; good enough for relative comparison)
PRICE_GB_S = 0.0000166667
PRICE_REQ = 0.0000002

# AAMF learning knobs
ALPHA = 0.3          # EMA step for consolidating an episode into memory
TAU = 1.0            # neighbour-context kernel width (in work levels)
RADIUS = 2           # how many neighbouring contexts are recalled
UCB_C = 0.6          # strength of the uncertainty bonus (exploration)
N_CAP = 20           # confidence cap so memory can still move
SURPRISE_T = 0.8     # |reward - remembered| above this triggers forgetting
FORGET_N = 2         # confidence a memory is reset to when surprised

SESSION_TTL_S = 3600
