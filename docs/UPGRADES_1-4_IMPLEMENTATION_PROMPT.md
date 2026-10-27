# AAMF upgrades 1–4: implementation prompt and file map

Use this prompt when continuing the AAMF work. It describes the requested code changes and the expected behavior in this repository.

## Copy-ready prompt

> Upgrade the AAMF project in small, reviewable file-by-file changes. Preserve the existing Flask + static JavaScript architecture and the current DynamoDB table key schema `(ctx, config)`; do not provision AWS resources or deploy code as part of editing local files.
>
> Implement these four engine improvements:
>
> 1. Replace scalar-only workload recall with a normalized feature vector containing `payload_size_kb`, UTC time of day, `request_type`, `queue_depth`, and `cold_start_rate` (percentage values 0–100). Keep the existing numeric DynamoDB key schema for compatibility, but derive a stable key for each discretized feature cluster and store the coarse work level as an attribute. Compute vector similarity across neighboring clusters and weight evidence by similarity and sample count.
> 2. Store the latency/cost Pareto frontier per workload/tier memory cell. Accept an operator-set runtime cost weight from 0 to 1. Select from the nondominated frontier using that weight without discarding the underlying latency and cost measurements.
> 3. Replace a one-observation surprise threshold with Page–Hinkley change detection. Track detector state per context/tier, trigger only after a minimum number of samples, selectively reset that cell, and persist the detector statistic and explanation in the episode audit record.
> 4. Fit a small weighted CPU-time-plus-wait regression from recent AAMF episodes. Estimate the counterfactual latency/cost of every tier for the current feature vector, use it as a cautious prior (not as measured truth), and expose sample count and the model label to the UI.
>
> Add these experiment and explanation components:
>
> - A live SVG memory graph showing the current request, the similar memory contexts used, evidence weight, sample count, and selected tier. Refresh it on every AAMF decision and reconstruct it from the newest stored episode after page reload.
> - A drift lab with no drift, a step increase, gradual increase, intermittent spikes, and alternating 3-request phases. Make the injected wait explicit in the progress status and keep the same traffic characteristics across the three controller comparisons.
> - CSV feature-trace replay with required headers `payload_size_kb,request_type,queue_depth,cold_start_rate` (percentage values 0–100), optional `work,extra_ms`, input validation, and a bounded row count. In real AWS mode, show a confirmation that states the row count, the three-controller invocation count, region, and that charges may apply. Never read or upload production payload bodies or credentials.
> - An explainable audit expansion for every AAMF decision: unique audit ID, objective weights, observed latency/cost, top tier estimates, measured-sample confidence interval, contributing neighbor similarity/weight/sample count, counterfactual model sample count, and a clear Page–Hinkley reset reason.
>
> Keep cold-start rate as a context signal; do not claim that setting it forces AWS Lambda cold starts. Never present counterfactual results as actual measurements. Do not silently turn this experiment into production traffic routing or live Lambda configuration changes. Preserve demo mode and legacy policy rows.
>
> Update `backend/config.py`, `backend/engine.py`, `backend/memory.py`, `backend/app.py`, `backend/invoker.py`, `target/handler.py`, `frontend/index.html`, `frontend/app.js`, and `frontend/style.css` as needed. Update README documentation and create this prompt/file map. Work in one file at a time and summarize the visible change after each file group. Do not run AWS mutations or deployment commands. If asked to verify, check Python/JavaScript syntax and exercise the documented demo path.

## Current file map

| File | Responsibility |
|---|---|
| `backend/config.py` | Feature names/scales, objective and Page–Hinkley settings |
| `backend/engine.py` | Vector similarity, Pareto selection, regression prior, drift detection, evidence |
| `backend/memory.py` | Nested DynamoDB serialization, persistent policy/frontier samples, audit episodes |
| `backend/app.py` | Validates step inputs and stores complete decision evidence in each episode |
| `backend/invoker.py` | Sends feature traces to real Lambda and models them in demo mode |
| `target/handler.py` | Makes payload size, operation class, queue depth, and injected wait affect work |
| `frontend/index.html` | Drift/objective/feature/CSV controls and memory graph component |
| `frontend/app.js` | Replays deterministic or CSV rows, updates the graph, expands audit evidence |
| `frontend/style.css` | Responsive styling for new controls and evidence views |
| `README.md` | User workflow, CSV schema, behavior, and scope limits |

## Data and behavior notes

- Feature values are metadata, not request bodies. The CSV replay sends representative feature rows into AAMF's benchmark Lambda handler.
- Each CSV row is sent to all three controllers. At a 250-row cap, a real run can invoke Lambda up to 750 times; the UI requires an explicit confirmation first.
- `cold_start_rate` (percentage values 0–100) contributes to context similarity but AWS controls whether an invocation is cold. The demo simulator can sample the configured rate.
- Counterfactual estimates come from a fitted model over recent AAMF episodes. Confidence intervals shown in the decision evidence are based on remembered measured samples, not on the regression.
- Page–Hinkley resets only one `(feature cluster, tier)` memory cell. Its prior episodes remain in the append-only episode log until normal TTL expiry or an explicit memory wipe.
- The DynamoDB feature rows retain the existing numeric `(ctx, config)` key types; `ctx` is a stable feature-cluster ID and `work_level` is an item attribute. Policy rows are scanned into a 60-second in-process cache to find nearby vectors, which is intended for this prototype’s small table. The dashboard compares three controllers by invoking three Lambda targets; it is not a production canary, traffic router, or automatic live configuration controller.
- Local code edits do not update already-deployed Lambda code. To deploy the changed benchmark handler, the operator must explicitly run the existing setup/update workflow in the selected AWS Region after reviewing its AWS effects.
