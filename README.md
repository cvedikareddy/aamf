# AAMF — Adaptive Architectural Memory Framework

AAMF is an interactive research demonstrator for **learning which cloud architecture configuration works best for different workload contexts**. It compares an adaptive, memory-based controller with two simple baselines, measures the result, and retains experience so later decisions can improve.

The current experiment tunes **AWS Lambda memory size** across five separately deployed benchmark functions. A Flask application serves the web dashboard and JSON API. In demo mode it simulates Lambda execution and stores memory in process; in AWS mode it invokes the Lambda functions and persists policy and episode data in DynamoDB.

> **Scope:** AAMF is a local experiment and observability dashboard, not a production traffic router or an automatic production optimizer. It invokes dedicated benchmark functions; it does not route user traffic or change a production Lambda's configuration.

## Contents

- [Problem statement](#problem-statement)
- [Core idea](#core-idea)
- [Architecture](#architecture)
- [Decision and learning workflow](#decision-and-learning-workflow)
- [Dashboard and experiments](#dashboard-and-experiments)
- [Components](#components)
- [AWS resources and permissions](#aws-resources-and-permissions)
- [Run locally](#run-locally)
- [Connect and set up AWS](#connect-and-set-up-aws)
- [API overview](#api-overview)
- [Limitations and safety notes](#limitations-and-safety-notes)

## Problem statement

Cloud workloads do not all benefit from the same amount of compute. A small Lambda memory allocation can reduce cost but increase latency; a larger allocation can reduce CPU-bound execution time while increasing per-millisecond cost. Payload size, request type, queue depth, time of day, and changing downstream waits can all affect the best choice.

Common approaches leave a gap between decisions and durable learning:

- **Static sizing** selects one configuration and applies it to every request.
- **Threshold rules** react to recent measurements but generally retain little decision-level evidence.
- **Offline tuning** can compare configurations for a sample workload, but its result can become stale as context or operating conditions change.

AAMF explores whether a small controller can make this trade-off more context-aware by remembering measurements, transferring evidence from similar contexts, and detecting when its stored experience stops being representative.

## Core idea

For each request, AAMF represents the workload as a feature vector and chooses a Lambda memory tier. After executing the benchmark, it records the observed latency and estimated Lambda cost as an **episode**, then updates a persistent memory cell for that workload cluster and tier.

The next decision can use:

1. **Direct experience** for the current feature cluster and tier.
2. **Transferred experience** from similar workload contexts, weighted by feature similarity and sample count.
3. **A cautious counterfactual prior** from a weighted regression fitted to recent measured AAMF episodes, when enough data and tier variation exist.
4. **An exploration bonus** for tiers with limited evidence.

The controller retains latency and cost measurements on a per-tier **Pareto frontier**: a point is kept when no other point is at least as good in both latency and cost and strictly better in one. The operator's cost-weight setting determines how AAMF scores the available evidence; it does not erase the underlying measurements.

When Page–Hinkley change detection observes a sustained increase in normalized loss for a particular workload-cluster/tier cell, AAMF resets that cell's learned summary and frontier so it can relearn. The episode history remains available until its normal expiry.

### Workload context

The decision features are:

| Feature | Meaning |
|---|---|
| `payload_size_kb` | Representative payload size in kilobytes; no payload body is read or replayed. |
| `hour_utc` | UTC hour captured by the browser or supplied by the backend. |
| `request_type` | Operation category such as `read`, `write`, `batch`, or `search`. |
| `queue_depth` | A workload pressure signal used in similarity and benchmark wait modeling. |
| `cold_start_rate` | Context signal. UI/CSV inputs use percentages from 0 to 100; the API and engine use a fraction from 0 to 1. It does **not** force an AWS Lambda cold start. |

`work` is a coarse workload intensity from 1 to 6. The engine derives a stable numeric feature-cluster identifier for the existing DynamoDB partition-key schema and stores the original coarse work level and representative features as item attributes.

## Architecture

```text
┌───────────────────────────────────────────────┐
│ Browser: static HTML, CSS, JavaScript         │
│ Dashboard · experiment controls · charts      │
└──────────────────────┬────────────────────────┘
                       │ HTTP / JSON, signed session cookie
┌──────────────────────▼────────────────────────┐
│ Flask app (`backend/app.py`)                    │
│ Session handling · API validation · orchestration│
└──────────┬─────────────────────┬───────────────┘
           │                     │
┌──────────▼───────────┐  ┌──────▼────────────────┐
│ Engine and memory    │  │ Invocation adapter    │
│ `engine.py`          │  │ `invoker.py`          │
│ decide · learn       │  │ simulator or Lambda   │
└──────────┬───────────┘  └──────┬────────────────┘
           │                     │
  Demo: LocalMemory       Demo: simulated result
  AWS: DynamoDB            AWS: five Lambda targets
  policy + episodes        128 / 256 / 512 / 1024 / 2048 MB
```

In AWS mode, `aws_session.py` holds the boto3 session on the server and associates it with an opaque identifier in a signed HttpOnly cookie. The browser does not receive AWS credentials back from the server. `aws_services.py` provides separate, read-oriented dashboard views for AWS inventory and service state.

### AWS resources used by the experiment

| Resource | Purpose |
|---|---|
| Five AWS Lambda functions | Identical benchmark handler deployed at 128, 256, 512, 1024, and 2048 MB. AAMF selects a function to invoke; it does not resize a production function. |
| `aamf-policy` DynamoDB table | Consolidated policy memory keyed by numeric `(ctx, config)`. `ctx` is a stable workload-cluster key and `config` is the Lambda tier; additional attributes hold features, counts, frontiers, and drift state. |
| `aamf-episodes` DynamoDB table | Recent decision and measurement audit records, keyed by `(stream, ts)`. Records are configured with a seven-day TTL. |
| `aamf-lambda-role` IAM role | Execution role for benchmark functions, with the AWS Lambda basic execution policy for logging. |
| AWS STS | Validates an AWS session with `GetCallerIdentity`. |

The AWS dashboard can also read EC2 instance inventory, Lambda CloudWatch metrics and AAMF-prefixed alarms, recent CloudTrail event history, and AWS Config recorder/rule compliance state. These are inspection views; the app does not create EC2 instances, CloudTrail trails, CloudWatch alarms, or AWS Config recorders.

## Decision and learning workflow

Each `/api/step` call performs one controller decision and one benchmark invocation:

```text
Browser                    Flask/API                 Engine, memory, target
   │                           │                              │
   ├─ policy + workload ──────>│                              │
   │                           ├─ validate/normalize context  │
   │                           ├─ recall similar memory ─────>│
   │                           ├─ select tier <───────────────┤
   │                           ├─ invoke selected tier ──────>│ Lambda or simulator
   │                           ├─ measure latency/cost <──────┤
   │                           ├─ learn and detect drift ────>│
   │                           ├─ append audit episode ──────>│ DynamoDB/local memory
   │<─ tier, result, evidence ─┤                              │
```

The adaptive decision process is:

1. **Normalize context.** Validate the workload inputs and prepare a feature vector.
2. **Recall neighbors.** Compare against stored feature clusters and weight evidence by similarity and sample count.
3. **Score candidate tiers.** Use remembered Pareto-frontier outcomes and a confidence/exploration bonus. Where available, blend in a low-weight counterfactual regression prior.
4. **Apply structural pruning.** Avoid tiers that are dominated by previously observed SLO failures at equal or heavier workloads, except where direct evidence supports trying them.
5. **Execute and measure.** Invoke the selected Lambda using `LogType=Tail` and parse its `REPORT` line, or use the simulator in demo mode.
6. **Learn.** Update the selected cell's frontier, samples, exponential moving summaries, and Page–Hinkley state.
7. **Audit.** Store the request features, decision source/reason, candidate evidence, measurement, cost, loss, and learning result.

The default objective uses `cost_weight=0.6`, so latency weight is 0.4 and cost weight is 0.6. For a request, normalized loss is:

```text
loss = latency_weight × latency_ms / SLO_ms
     + cost_weight × cost_µ$ / reference_cost_µ$
     + SLO_violation_penalty_if_latency_exceeds_SLO
```

The default SLO is 1500 ms. Values and learning parameters are centralized in [`backend/config.py`](backend/config.py).

### Controllers compared

All three controllers receive the same request context and injected wait for each experiment step:

| Controller | Behavior |
|---|---|
| **AAMF** | Recalls and learns from persistent context/tier experience; can transfer evidence, explore, and forget stale cell summaries. |
| **Threshold rule** | Reactive baseline: steps up when latency exceeds 80% of the SLO and steps down after three requests below 25% of the SLO. |
| **Static 512 MB** | Fixed-size baseline that always selects the 512 MB tier. |

## Dashboard and experiments

The web interface has a sidebar with the following views:

### Overview

- **Memory heatmap:** learned loss and sample count by coarse work level and tier; the best remembered tier is highlighted.
- **Controller scoreboard:** mean loss, latency, cost, SLO misses, and tier switches for the current experiment run.
- **Summary cards:** relative loss vs. static, learned memory cells, AAMF SLO misses, and forgetting events.
- **Charts:** per-request experiment loss and selected architecture tier.
- **Decision-source summary and recent log:** counts and explanations for exploit, transfer, and explore decisions.

The Overview's experiment charts summarize the current browser run. They are distinct from the AWS service view, which queries actual CloudWatch metrics.

### Live lab

- Run 30, 60, or 90 generated requests, or replay a CSV trace.
- Select one of four explicit injected-wait patterns: step increase, gradual increase, intermittent spikes, or alternating three-request phases. A no-drift option is also available.
- Adjust the cost-vs-latency objective weight from 0% to 100% cost.
- Set default payload size, request type, queue depth, and cold-start-rate percentage. UTC hour is captured automatically.
- Compare all three controllers on a shared request stream using the scoreboard and charts.
- Inspect the live memory graph, which links the current decision to similar context evidence and the selected tier.
- Expand AAMF audit entries to see the audit ID, objective weights, observed metrics, candidate estimates, confidence intervals, neighbor similarity/weight/sample counts, and any drift explanation.
- Stop the run or wipe policy memory and episode history for the current memory store.

In real AWS mode, every experiment request invokes all three controller targets. A run of `N` requests therefore makes `3 × N` Lambda invocations, and the dashboard asks for confirmation before starting because AWS charges may apply.

CSV input is a **feature trace**, not production request replay. Required headers are:

```csv
payload_size_kb,request_type,queue_depth,cold_start_rate
192,read,4,5
768,write,12,20
```

Optional columns are `work` (1–6) and `extra_ms`. The file is limited to 250 data rows. `cold_start_rate` in the CSV is a percentage from 0 to 100. Do not put credentials or production payload bodies in the CSV. Cold-start rate informs context similarity; it does not tell AWS to create a cold start.

Counterfactual tier estimates come from a weighted CPU-time-plus-wait regression over recent measured AAMF episodes. They are priors, not actual tier measurements. Confidence intervals are computed from remembered measured samples.

### Memory

Shows the learned architecture heatmap. In AWS mode the policy rows are read from DynamoDB; in demo mode they exist only for the lifetime of the in-process session.

### AWS resources

Lists AAMF-prefixed Lambda functions and DynamoDB tables in the connected Region, including function tier/runtime/state and table status/item count/billing mode. Links open the corresponding AWS Console pages.

### EC2 fleet

Reads non-terminated EC2 instance inventory in the connected Region (instance name/ID/type/state/availability zone/launch time). This is a read-only inventory page; it does not launch or modify instances.

### CloudWatch

Reads the last 30 minutes of Invocations, Errors, and average Duration metrics for AAMF Lambda functions, and lists alarms with the `aamf-` prefix. AAMF does not create alarms automatically.

### CloudTrail

Reads up to the latest 50 management events from event history in the connected Region. It does not create or configure a CloudTrail trail.

### AWS Config

Reads configuration recorder status and existing Config rule state/compliance. If no recorder is configured, the dashboard reports that state; it does not create a recorder.

### Setup

Connects a session, displays readiness, and offers an explicitly confirmed provisioning action for the AAMF role, five Lambda functions, and two DynamoDB tables in the selected Region. No AWS resources are provisioned just by opening the dashboard or connecting.

## Components

| Path | Responsibility |
|---|---|
| [`backend/app.py`](backend/app.py) | Flask app, static file serving, JSON endpoints, request validation, controller orchestration, and audit episode creation. |
| [`backend/config.py`](backend/config.py) | Resource names, tier list, context fields/scales/weights, SLO, cost model, session TTL, and learning/drift parameters. |
| [`backend/aws_session.py`](backend/aws_session.py) | Demo/AWS session creation, STS identity validation, resource readiness checks, server-side session store, and session expiry. |
| [`backend/engine.py`](backend/engine.py) | Feature normalization and similarity, cluster key generation, AAMF decision/learning logic, Pareto frontier, regression prior, Page–Hinkley drift detection, and the two baselines. |
| [`backend/memory.py`](backend/memory.py) | Thread-safe `LocalMemory` for demo sessions and `DynamoMemory` for persistent AWS policy rows and episode history. |
| [`backend/invoker.py`](backend/invoker.py) | Simulated invocation, real Lambda invocation, Lambda `REPORT` parsing, and per-invocation cost estimation. |
| [`backend/aws_services.py`](backend/aws_services.py) | Region-scoped read-only AWS inventory and service data for EC2, Lambda/DynamoDB, CloudWatch, CloudTrail, and Config. |
| [`frontend/index.html`](frontend/index.html) | Dashboard page structure, navigation, lab controls, tables, charts, and setup panel. |
| [`frontend/app.js`](frontend/app.js) | API/session wiring, experiment plan and CSV parsing, comparisons, charts, heatmap, audit log, and AWS view rendering. |
| [`frontend/style.css`](frontend/style.css) | Dashboard styling, layout, responsive behavior, and visual states. |
| [`target/handler.py`](target/handler.py) | Benchmark Lambda handler. CPU work scales with work/payload/type; a sleep component models downstream wait and queue depth. |
| [`infra/setup_aws.py`](infra/setup_aws.py) | Idempotent provisioning/updating of the AAMF Lambda role/functions/tables and episode TTL. |
| [`infra/teardown_aws.py`](infra/teardown_aws.py) | Deletes the AAMF resources created by setup. |
| [`infra/iam_policy.json`](infra/iam_policy.json) | Example permissions for provisioning and the dashboard's AWS read operations. Review before use. |
| [`docs/UPGRADES_1-4_IMPLEMENTATION_PROMPT.md`](docs/UPGRADES_1-4_IMPLEMENTATION_PROMPT.md) | Implementation history/context and behavior notes for the feature upgrades. |

## AWS resources and permissions

The provisioner creates or updates resources in the Region passed to it (default: `AWS_REGION` or `ap-southeast-2`). Use the Region assigned to the AWS project. Confirm it in **AWS Settings → View all projects → Overview → Additional Info → Region** before provisioning. All regional resources used by this project must be in that selected Region.

The provisioner creates:

- IAM role `aamf-lambda-role` for Lambda execution with basic logging.
- Lambda functions `aamf-target-128`, `aamf-target-256`, `aamf-target-512`, `aamf-target-1024`, and `aamf-target-2048`.
- On-demand DynamoDB tables `aamf-policy` and `aamf-episodes`; episode records use a seven-day TTL.

The setup permission policy in [`infra/iam_policy.json`](infra/iam_policy.json) includes resource creation/deletion permissions in addition to read permissions for dashboard views. Review and scope these permissions according to your environment before attaching them. The Lambda execution role itself only receives the AWS-managed basic execution policy.

AWS usage can incur charges, including Lambda invocations, DynamoDB requests/storage, and CloudWatch/other service requests. The CSV limit is 250 rows, which can mean up to 750 Lambda invocations for a complete three-controller comparison. The app binds to localhost by default; do not expose this prototype to an untrusted network.

## Run locally

### Prerequisites

- Python 3.10 or later.
- For demo mode: no AWS credentials or AWS resources are required.
- For AWS mode: AWS CLI credentials/profile with the required permissions and access to the selected Region.

### Install dependencies

From the repository root, in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### Start the dashboard

```powershell
.\.venv\Scripts\python.exe backend\app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The server binds to `127.0.0.1` and uses port 8000 by default; set the `PORT` environment variable to choose a different local port.

Choose **Demo session** on the Setup page to explore the workflow without AWS. Demo invocations are simulated and memory is in-process; it is cleared when that app session is lost or reset.

## Connect and set up AWS

### Authenticate

Use an authenticated AWS CLI profile or the default credential chain. For the AWS profile flow, authenticate using the CLI before connecting, for example:

```powershell
aws login --profile <your-profile>
```

Then in the Setup page:

1. Choose **AWS profile** and enter its profile name, or choose **Default credential chain**.
2. Enter the selected Region for the project.
3. Press **Connect**. AAMF validates identity through STS and checks for all five Lambda targets and both DynamoDB tables.
4. If resources are missing, review the resource list and explicitly confirm setup from the Setup page.
5. Open **Live lab** and run a test.

The dashboard also offers access-key entry for a session, but prefer a credential provider/profile where possible. Credentials are sent to the local Flask backend for session initialization and are not returned to the page. The backend keeps the boto3 session in process memory, associated with an opaque signed HttpOnly cookie, and expires it after one hour. This prototype's in-memory session store is not a shared production session service.

### Provision using the CLI (optional)

The provisioning script is idempotent and deploys `target/handler.py` to the five benchmark functions. Run it only after confirming the selected Region and reviewing its AWS effects:

```powershell
.\.venv\Scripts\python.exe infra\setup_aws.py --profile <your-profile> --region <selected-region>
```

To delete the AAMF resources, use the teardown script only when you intend to remove the listed project resources and their stored experiment data:

```powershell
.\.venv\Scripts\python.exe infra\teardown_aws.py --profile <your-profile> --region <selected-region>
```

Teardown deletes the five AAMF Lambda functions, both AAMF DynamoDB tables, and the AAMF Lambda execution role. It does not delete unrelated resources.

## API overview

The Flask app serves the frontend and exposes these JSON endpoints:

| Method and route | Purpose |
|---|---|
| `POST /api/connect` | Open a demo or AWS session and report resource readiness. |
| `POST /api/disconnect` | Close the current session. |
| `GET /api/status` | Read current session/readiness state. |
| `POST /api/provision` | Create or update required AAMF resources after an AWS connection. |
| `POST /api/step` | Decide, invoke, learn, and return a single controller's result and evidence. |
| `GET /api/memory` | Return policy memory, recent episodes, tiers, work levels, and objective constants. |
| `POST /api/reset` | Clear the current session's memory and recreate baseline/controller state. |
| `GET /api/aws/resources` | Read AAMF Lambda and DynamoDB inventory. |
| `GET /api/aws/ec2` | Read EC2 instance inventory. |
| `GET /api/aws/cloudwatch` | Read Lambda metrics and AAMF-prefixed alarms. |
| `GET /api/aws/cloudtrail` | Read recent CloudTrail event history. |
| `GET /api/aws/config` | Read AWS Config recorder and rule status. |

`POST /api/step` accepts `policy` (`aamf`, `threshold`, or `static`), `work` (1–6), optional `context`, `extra_ms` (bounded to 0–2000 by the API), and `cost_weight` (0–1). It returns the selected tier, observed latency/billed duration/cost, loss/SLO result, decision evidence, and learning result. In real AWS mode, AAMF policy decisions require the configured DynamoDB tables and Lambda functions to be present.

## Limitations and safety notes

- This is a prototype and the benchmark handler is synthetic. Results are not a recommendation for unrelated production functions or workloads.
- The controller compares fixed, separately deployed memory tiers. It does not dynamically update a live function, manage concurrency, or route application traffic.
- Real mode executes a Lambda for every controller/request pair; repeated experiments can generate AWS charges.
- The AWS session store and Flask secret are process-local. For a local deployment the generated secret is sufficient for the demo, but do not use this app as-is for a public or multi-worker deployment.
- The DynamoDB policy table is scanned and cached for 60 seconds to find nearby feature clusters. That is suitable only for small experiments; a production-scale design needs an appropriate index or retrieval strategy.
- The episode table uses one global partition key for simplicity. A production workload would need a more scalable partitioning strategy.
- Policy updates use straightforward writes and are not designed for multiple concurrent controller writers.
- Counterfactual outputs are model estimates, not measured outcomes. Cold-start-rate input is contextual metadata and does not control Lambda's cold-start behavior.
- Editing `target/handler.py` locally does not change deployed functions. Run the provision/update workflow explicitly, after reviewing the selected Region and AWS effects, to update the benchmark targets.
- “Wipe memory” clears the current policy and episode store. In AWS mode that is destructive to AAMF experiment history in the two configured tables.
