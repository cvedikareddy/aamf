# AAMF: Adaptive Architectural Memory Framework

A small, runnable demonstration of a cloud system that **remembers which architecture configuration worked under which workload**, reuses that knowledge for conditions it has never seen, and **forgets quickly when the world changes**.

No Docker, no frameworks beyond Flask and Chart.js. One Python backend, one static site, five Lambda functions and two DynamoDB tables.

---

## 1. The idea in one paragraph

Most cloud tuning is either *static* (a human picks a size once) or *reactive* (an alarm fires, a step is taken, nothing is retained). AAMF adds an **architectural memory** layer: every decision (workload context, chosen configuration, measured latency and cost) is written to DynamoDB as an episode, consolidated into a per-(context, configuration) belief, and recalled on the next decision. The memory is persistent, queryable, and explainable, and it outlives the controller process.

Three mechanisms sit on top of the memory:

| Mechanism | What it does | Where |
|---|---|---|
| **Neighbour-context transfer** | An unseen workload level inherits beliefs from nearby levels, distance-weighted. No cold start per context. | `engine.AAMF.decide` |
| **Structure-aware exploration** | Hill-climbs over ordered tiers and prunes tiers that provably break the SLO (if tier T missed the SLO at load L, smaller tiers at load ≥ L are skipped, not tried). | `engine.AAMF.decide` |
| **Surprise-gated forgetting** | If an outcome is far from what memory predicted, confidence in that memory is cut so the system re-learns instead of trusting a stale belief. | `engine.AAMF.learn` |

Every decision comes back with a reason (`explore`, `transfer`, `exploit`, pruned tiers, forgetting events), so the memory is an audit trail rather than a black box.

> **Honest scope note.** Bandit-style tuning and RL autotuners exist in research and products. The contribution here is the *framing and packaging*: architecture as a persistent, inspectable memory in a managed store, combined with context transfer, structural pruning and surprise-gated forgetting, running against real AWS billing data. It is a demonstrator, not a production controller.

---

## 2. What the website does

Open it, pick a session mode (demo or a real AWS session), and press **Run**. Three controllers receive the **identical** request stream against the real Lambda tiers (128 / 256 / 512 / 1024 / 2048 MB):

1. **AAMF**: the adaptive memory controller.
2. **Threshold rule**: a CloudWatch-alarm-style step rule (scale up on pressure, down after calm).
3. **Static 512 MB**: a size picked by a human.

The workload streams light → heavy → random mix → steady. You see, live: loss per request, which tier each controller picked, a heatmap of what AAMF has stored in DynamoDB, and a decision log explaining each AAMF choice. A checkbox injects a +400 ms downstream slowdown mid-run so you can watch surprise-gated forgetting fire. Run twice without wiping memory and AAMF starts the second run already tuned.

Latency and cost come from the Lambda `REPORT` line (billed duration and memory) returned by `Invoke(LogType=Tail)`, i.e. AWS's own numbers, not client-side stopwatch noise.

---

## 3. Repo structure

```
aamf/
├── README.md
├── .gitignore
├── .env.example
├── infra/
│   ├── setup_aws.py        # creates IAM role, 5 Lambdas, 2 DynamoDB tables (idempotent)
│   ├── teardown_aws.py     # deletes everything it created
│   └── iam_policy.json     # least-privilege policy for the user running this
├── target/
│   └── handler.py          # the workload deployed to every Lambda tier
├── backend/
│   ├── requirements.txt    # flask, boto3
│   ├── config.py           # names, tiers, reward weights, learning knobs
│   ├── aws_session.py      # opens/validates boto3 sessions from the website, TTL store
│   ├── memory.py           # DynamoMemory (real) + LocalMemory (demo mode)
│   ├── engine.py           # AAMF + the two baselines
│   ├── invoker.py          # Lambda invoke + REPORT parsing + cost; simulator for demo mode
│   └── app.py              # Flask: serves site + JSON API
└── frontend/
    ├── index.html
    ├── style.css
    └── app.js              # session panel, run loop, Chart.js charts, heatmap, log
```

---

## 4. AWS services used (and why)

| Service | Role in AAMF |
|---|---|
| **AWS Lambda** (5 functions) | The system being tuned. Each memory tier is a separate function so switching architecture is instant (production would call `UpdateFunctionConfiguration`). |
| **Amazon DynamoDB** (2 tables, on-demand) | `aamf-policy` = consolidated memory, key `(ctx, config)`. `aamf-episodes` = raw experience log, 7-day TTL. |
| **AWS IAM** | One execution role for the Lambdas; a least-privilege policy for you (`infra/iam_policy.json`). |
| **AWS STS** | Validates the session you open from the website (`GetCallerIdentity`). |

Cost for a demo session is a few cents or less (on-demand DynamoDB, short Lambda runs). Run `teardown_aws.py` when finished.

---

## 5. Setup, step by step

**Prerequisites:** Python 3.10+, an AWS account, credentials configured locally (`aws configure`, an SSO profile, or access keys).

```bash
# 1. permissions: attach infra/iam_policy.json to the IAM user/role you will use

# 2. install
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# 3. provision AWS (about a minute)
python infra/setup_aws.py --profile <your-profile> --region ap-south-1

# 4. run the site
python backend/app.py                                    # http://127.0.0.1:8000
```

In the browser: **Open a session** → choose *AWS profile* (name of your profile and region) or *Default credential chain*, press **Connect**. The panel shows your account ID and whether all resources were found. Then press **Run**.

No AWS account handy? Choose **Demo**: same site, simulated Lambda, in-process memory. Good for a quick look, but the real-AWS mode is the point of the project.

Teardown:

```bash
python infra/teardown_aws.py --profile <your-profile> --region ap-south-1
```

### How the session connection works

The browser submits a mode (profile, keys, or default chain) to `POST /api/connect`. The backend builds a `boto3.Session`, verifies it with STS, checks the five functions and two tables exist, and keeps the session in **server memory** keyed by an opaque id carried in a signed HttpOnly cookie. Credentials are never returned to the page, expire after one hour, and the server binds to `127.0.0.1` only. Do not expose it on a network.

---

## 6. Workflow (one decision cycle)

```
 browser ──POST /api/step {policy, work}──▶ Flask
                                              │
   1 RECALL    memory.neighbors(ctx ±2)  ───▶ DynamoDB aamf-policy (Query per nearby ctx)
   2 DECIDE    score(tier) = remembered reward + uncertainty bonus
               prune SLO-dominated tiers, hill-climb unseen ones
   3 ACT       lambda.invoke(aamf-target-<tier>, LogType=Tail) ─▶ Lambda
   4 MEASURE   parse REPORT: duration, billed ms ─▶ latency, cost (µ$)
   5 LEARN     surprise? → cut confidence │ EMA update ─▶ DynamoDB aamf-policy
   6 LOG       episode + reason ─▶ DynamoDB aamf-episodes
                                              │
 browser ◀──{tier, latency, cost, loss, reason}┘
```

Reward: `loss = 0.4·latency/SLO + 0.6·cost/ref`, plus 1.0 if the SLO (1500 ms) is missed. Weights live in `config.py`; change them to express a different business objective.

---

## 7. How it compares with existing approaches

These descriptions reflect how the approaches are commonly documented; check current AWS docs for specifics.

| Approach | How it decides | Retains experience? | Context-aware? | Adapts to drift | Limitation AAMF targets |
|---|---|---|---|---|---|
| Manual sizing | A human guesses once | No | No | No | Goes stale; one size for all requests |
| CloudWatch alarms, step / target-tracking scaling | Reactive threshold on a live metric | No (only current state and cooldown) | No | Only by oscillating | No learning; thrashes on alternating load |
| Predictive scaling (EC2 Auto Scaling) | Forecasts load from history | Yes, as a time-series forecast | Time only | Retrained periodically | Forecasts *how much*, not *which architecture* |
| AWS Lambda Power Tuning | Offline sweep of memory sizes for one payload | One-shot snapshot | One payload | Manual re-run | Not continuous, not per-request-context |
| AWS Compute Optimizer | Periodic recommendations from a historical lookback | Yes, as a lookback window | Aggregate | Slowly | Advisory, not a closed loop |
| **AAMF** | **Recall, score, act, learn, forget** | **Yes: persistent, queryable memory** | **Yes: with neighbour transfer** | **Yes: surprise-gated forgetting** | Demonstrator scale; see below |

### What to expect from the numbers

In the bundled simulator (30-seed averages over a 60-request stream) AAMF's blended loss is roughly 9-13% lower than a reactive threshold rule and 15-19% lower than static 512 MB (cold vs warm memory), and a warm memory (second run) beats a cold one. Real AWS numbers will differ. Three honest caveats:

- Under an injected slowdown AAMF accepts **more SLO misses** than the threshold rule because it optimises the *blended* objective; the threshold rule buys fewer misses with higher cost. Raise `VIOLATION_PENALTY` if SLO is paramount.
- In a short stationary run a good reactive rule is competitive; memory pays off over longer horizons, with alternating contexts, and across restarts.
- Results depend on the reward weights, so treat them as a statement about *your* objective.

---

## 8. Limits and next steps

- **Single writer.** `put_item` last-write-wins is fine for one controller; use conditional writes or atomic `ADD` counters for several.
- **Scan-free reads.** Recall queries only neighbouring contexts, so reads stay constant-size as the table grows. The `snapshot` call used by the heatmap does scan; it is a demo view.
- **One knob.** Memory size only. The same memory schema generalises to (concurrency, instance type, cache size, batch size) with a richer context vector.
- **Hot partition.** The episodes log uses one partition key for simplicity; shard it by day for real traffic.
- **Next:** drive `UpdateFunctionConfiguration` on a single function, add CloudWatch Embedded Metrics for context features, and gate changes behind a canary alias.
