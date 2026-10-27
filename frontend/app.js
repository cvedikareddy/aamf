/* AAMF frontend: session panel, live run loop, charts, memory heatmap, decision log. */
const POLICIES = [
  { id: "aamf",      label: "AAMF (adaptive memory)",        color: "#3479d1" },
  { id: "threshold", label: "Threshold rule (CloudWatch-style)", color: "#e8783e" },
  { id: "static",    label: "Static 512 MB",                 color: "#8996a8" },
];
const $ = (id) => document.getElementById(id);

let running = false, connected = false, provisioning = false, sessionInfo = null, data, plan = [];

function resetData() {
  data = {};
  POLICIES.forEach((p) => (data[p.id] = { loss: [], tier: [], lat: 0, cost: 0, viol: 0, n: 0, sw: 0, last: null }));
}

async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function escHtml(value) {
  return String(value == null ? "—" : value).replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
}

function serviceMessage(id, message, isError = false) {
  const el = $(id);
  if (el) el.innerHTML = '<div class="service-message' + (isError ? ' err' : '') + '">' + escHtml(message) + '</div>';
}

function serviceTable(id, columns, rows, emptyMessage) {
  const el = $(id);
  if (!el) return;
  if (!rows.length) { serviceMessage(id, emptyMessage); return; }
  const head = columns.map(c => '<th>' + escHtml(c.label) + '</th>').join('');
  const body = rows.map(row => '<tr>' + columns.map(c => '<td>' + escHtml(c.value(row)) + '</td>').join('') + '</tr>').join('');
  el.innerHTML = '<div class="table-scroll"><table class="service-table"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
}

function loadVisibleAwsView() {
  const active = document.querySelector('.view.active');
  if (!active) return Promise.resolve();
  const name = active.id.replace('view-', '');
  return loadAwsView(name);
}

async function loadAwsView(view) {
  const endpoint = { resources: 'resources', fleet: 'ec2', cloudwatch: 'cloudwatch', cloudtrail: 'cloudtrail', config: 'config' }[view];
  if (!endpoint) return;
  const targets = { resources: ['lambdaInventory','dynamoInventory'], fleet: ['ec2Inventory'], cloudwatch: ['cwMetrics','cwAlarms'], cloudtrail: ['trailEvents'], config: ['configStatus'] }[view];
  if (!connected || !sessionInfo || sessionInfo.mode === 'demo') {
    targets.forEach(id => serviceMessage(id, sessionInfo && sessionInfo.mode === 'demo' ? 'Choose an AWS profile or credentials to load live data.' : 'Connect to AWS to load live data.'));
    return;
  }
  targets.forEach(id => serviceMessage(id, 'Loading live data from ' + sessionInfo.region + '…'));
  try {
    const result = await api('/api/aws/' + endpoint);
    if (view === 'resources') {
      $('resourceRegion').textContent = result.region;
      const region = encodeURIComponent(result.region);
      $('lambdaConsoleLink').href = 'https://' + result.region + '.console.aws.amazon.com/lambda/home?region=' + region + '#/functions';
      $('dynamoConsoleLink').href = 'https://' + result.region + '.console.aws.amazon.com/dynamodbv2/home?region=' + region + '#tables';
      serviceTable('lambdaInventory', [
        {label:'Function',value:r=>r.name},{label:'Memory',value:r=>r.memory_mb + ' MB'},
        {label:'Runtime',value:r=>r.runtime},{label:'State',value:r=>r.state},{label:'Timeout',value:r=>r.timeout_s + ' s'}
      ], result.lambdas, 'No AAMF Lambda functions found in this region. Use Setup to create them.');
      serviceTable('dynamoInventory', [
        {label:'Table',value:r=>r.name},{label:'Status',value:r=>r.status},{label:'Items',value:r=>r.item_count},
        {label:'Billing',value:r=>r.billing_mode}
      ], result.tables, 'No AAMF DynamoDB tables found in this region. Use Setup to create them.');
    } else if (view === 'fleet') {
      $('ec2Count').textContent = result.instances.length + ' instances';
      serviceTable('ec2Inventory', [
        {label:'Name',value:r=>r.name || '—'},{label:'Instance ID',value:r=>r.id},{label:'Type',value:r=>r.type},
        {label:'State',value:r=>r.state},{label:'Availability zone',value:r=>r.az},{label:'Launched',value:r=>r.launch_time}
      ], result.instances, 'No active or stopped EC2 instances were found in this region.');
    } else if (view === 'cloudwatch') {
      const rows = [];
      result.metrics.forEach(m => {
        const point = m.points.length ? m.points[m.points.length - 1] : null;
        rows.push({function:m.function, metric:m.metric, value:point ? (m.metric === 'Duration' ? Number(point.value).toFixed(1) + ' ms' : point.value) : 'No datapoints', time:point ? point.time : '—'});
      });
      serviceTable('cwMetrics', [
        {label:'Lambda function',value:r=>r.function},{label:'Metric',value:r=>r.metric},{label:'Latest value',value:r=>r.value},{label:'Timestamp',value:r=>r.time}
      ], rows, 'No AAMF Lambda functions found, so CloudWatch has no AAMF metrics to query yet.');
      serviceTable('cwAlarms', [
        {label:'Alarm',value:r=>r.name},{label:'State',value:r=>r.state},{label:'Metric',value:r=>r.metric},{label:'Reason',value:r=>r.reason || '—'}
      ], result.alarms, 'No AAMF alarms found.');
    } else if (view === 'cloudtrail') {
      serviceTable('trailEvents', [
        {label:'Time',value:r=>r.time},{label:'Event',value:r=>r.name},{label:'AWS service',value:r=>r.source},
        {label:'Identity',value:r=>r.user},{label:'Resources',value:r=>r.resources.join(', ')}
      ], result.events, 'CloudTrail returned no recent management events in this region.');
    } else if (view === 'config') {
      if (!result.configured) {
        serviceMessage('configStatus', 'AWS Config has no configuration recorder in ' + result.region + '. This dashboard only reads Config; enable a recorder in the AWS Console if you want compliance data.');
      } else {
        const recorders = result.recorders.map(r => '<div class="config-recorder"><b>' + escHtml(r.name) + '</b><span class="state-pill ' + (r.recording ? 'good' : 'warn') + '">' + (r.recording ? 'Recording' : 'Stopped') + '</span><small>Last status: ' + escHtml(r.last_status || 'Unknown') + '</small></div>').join('');
        $('configStatus').innerHTML = '<article class="card"><div class="card-heading"><div><h2>Configuration recorders</h2><p>' + escHtml(result.region) + '</p></div></div>' + recorders + '<div class="card-heading config-rules-heading"><div><h2>Config rules</h2><p>Current compliance evaluation</p></div></div><div id="configRules"></div></article>';
        serviceTable('configRules', [
          {label:'Rule',value:r=>r.name},{label:'State',value:r=>r.state},{label:'Compliance',value:r=>r.compliance}
        ], result.rules, 'No Config rules are configured.');
      }
    }
  } catch (e) {
    targets.forEach(id => serviceMessage(id, e.message, true));
  }
}
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 6000);
}

/* ---------- session panel ---------- */
function syncMode() {
  const m = $("mode").value;
  document.querySelectorAll(".f-profile").forEach((e) => (e.style.display = m === "profile" ? "" : "none"));
  document.querySelectorAll(".f-keys").forEach((e) => (e.style.display = m === "keys" ? "" : "none"));
  document.querySelectorAll(".f-aws").forEach((e) => (e.style.display = m === "demo" ? "none" : ""));
}

function syncProvisionState(s = sessionInfo) {
  const button = $("btnProvision");
  const status = $("provisionStatus");
  const awsConnected = !!(s && s.connected && s.mode !== "demo");
  const missing = s && Array.isArray(s.missing) ? s.missing : [];
  if (!awsConnected) {
    button.disabled = true; button.textContent = "Connect to AWS first";
    status.textContent = s && s.mode === "demo" ? "Switch from Demo to an AWS profile or credentials first." : "Connect an AWS account to enable resource setup.";
  } else if (!missing.length) {
    button.disabled = true; button.textContent = "AAMF resources ready";
    status.textContent = "All required Lambda functions and DynamoDB tables are available.";
  } else {
    button.disabled = provisioning; button.textContent = provisioning ? "Creating resources…" : "Create required resources";
    status.textContent = provisioning ? "Provisioning in the connected AWS account. This can take a few minutes…" : `Missing ${missing.length} AAMF resource${missing.length === 1 ? "" : "s"}: ${missing.join(", ")}`;
  }
}

async function provisionResources() {
  if (!sessionInfo || !connected || sessionInfo.mode === "demo") return;
  const region = sessionInfo.region;
  const account = sessionInfo.account ? ` account ${sessionInfo.account}` : "";
  const ok = window.confirm(`Create/update AAMF resources in ${region}${account}?

This will create or update:
• IAM role aamf-lambda-role with basic Lambda logging
• Five Lambda functions (128–2048 MB)
• Two on-demand DynamoDB tables (episode records expire after 7 days)

AWS usage can incur charges. No EC2, CloudTrail, CloudWatch alarms, or AWS Config resources will be created.

Continue?`);
  if (!ok) return;
  provisioning = true; syncProvisionState();
  try {
    const s = await api("/api/provision", {});
    showConn(s); await refreshMemory(); await loadVisibleAwsView();
    $("progress").textContent = "AAMF resources are ready. You can start a live lab run.";
    toast("AAMF resources created and verified in AWS.");
  } catch (e) {
    $("provisionStatus").className = "status err"; $("provisionStatus").textContent = e.message;
    toast(`AWS setup stopped: ${e.message}`);
  } finally {
    provisioning = false;
    try { sessionInfo = await api("/api/status"); showConn(sessionInfo); } catch (_) { syncProvisionState(); }
  }
}

function showConn(s) {
  sessionInfo = s; connected = !!s.connected;
  const el = $("conn");
  if (!connected) { el.className = "status"; el.textContent = "Not connected."; $("sideSession").textContent="Demo session"; $("sideRegion").textContent="Simulated services"; $("sideMemory").textContent="in-process (demo)"; $("topAccount").textContent="Demo session"; }
  else {
    const who = s.mode === "demo" ? "Demo session (simulated Lambda, in-process memory)" :
      `AWS account ${s.account} · ${s.region}`;
    el.className = "status ok";
    el.textContent = `Connected: ${who} · memory: ${s.memory}`;
    $("sideSession").textContent=s.mode==="demo"?"Demo session":`AWS account ${s.account}`; $("sideRegion").textContent=s.mode==="demo"?"Simulated services":s.region; $("sideMemory").textContent=s.memory||"Connected"; $("topAccount").textContent=s.mode==="demo"?"Demo session":`AWS · ${s.account}`;
    if (!s.ready) {
      el.className = "status err";
      el.textContent += ` · ${s.missing.length} AAMF resources are missing. Use Setup to create them.`;
    }
  }
  const ready = connected && s.ready !== false;
  $("btnConnect").disabled = connected;
  $("btnDisconnect").disabled = !connected;
  $("btnStopSession").hidden = !connected;
  $("btnRun").disabled = !ready || running;
  syncProvisionState(s);
  $("btnReset").disabled = !ready || running;
}

async function connect() {
  $("btnConnect").disabled = true;
  try {
    const s = await api("/api/connect", {
      mode: $("mode").value, region: $("region").value.trim(), profile: $("profile").value.trim(),
      access_key: $("ak").value.trim(), secret_key: $("sk").value, session_token: $("st").value,
    });
    $("sk").value = ""; $("st").value = "";           // do not keep secrets in the page
    showConn(s); await refreshMemory(); await loadVisibleAwsView();
  } catch (e) { toast(e.message); try { showConn(await api("/api/status")); } catch (_) { showConn({ connected: false }); } $("conn").className = "status err"; $("conn").textContent = e.message; if (!connected) $("btnConnect").disabled = false; }
}

async function disconnect() {
  running = false;
  try { await api("/api/disconnect", {}); }
  finally {
    showConn({ connected: false });
    $("heat").innerHTML = ""; $("labHeat").innerHTML = ""; $("memoryFull").innerHTML = "";
    $("log").innerHTML = ""; $("labLog").innerHTML = "";
    $("progress").textContent = "Session stopped. Choose a connection method to open a new session.";
  }
}

/* ---------- workload plan (deterministic, same for every controller) ---------- */
function makePlan(n) {
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const out = [];
  for (let i = 0; i < n; i++) {
    const p = i / n;
    out.push(p < 0.25 ? 1 + (i % 2) : p < 0.5 ? 5 + (i % 2) : p < 0.75 ? 1 + Math.floor(rnd() * 6) : 3);
  }
  return out;
}

/* ---------- charts ---------- */
let cLoss, cTier, labLoss, labTier;
function lineChart(el, opts = {}) {
  return new Chart(el, {
    type: "line",
    data: { labels: [], datasets: POLICIES.map((p) => ({
      label: p.label, data: [], borderColor: p.color, backgroundColor: p.color,
      borderWidth: 2, pointRadius: 0, tension: 0.25, stepped: opts.stepped || false })) },
    options: {
      animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#58677d" } } },
      scales: {
        x: { ticks: { color: "#8a96a7", maxTicksLimit: 10 }, grid: { color: "#e8edf3" }, title: { display: true, text: "request #", color: "#8a96a7" } },
        y: { ticks: { color: "#8a96a7" }, grid: { color: "#e8edf3" }, ...(opts.y || {}) },
      },
    },
  });
}

const ma = (arr, k = 4) => arr.map((_, i) => {
  const s = arr.slice(Math.max(0, i - k + 1), i + 1); return s.reduce((a, b) => a + b, 0) / s.length;
});

function drawCharts() {
  const n = data.aamf.loss.length;
  const labels = Array.from({ length: n }, (_, i) => i + 1);
  cLoss.data.labels = labels; cTier.data.labels = labels;
  POLICIES.forEach((p, i) => {
    cLoss.data.datasets[i].data = ma(data[p.id].loss);
    cTier.data.datasets[i].data = data[p.id].tier;
  });
  cLoss.update("none"); cTier.update("none");
  if (labLoss) { labLoss.data.labels = labels; labTier.data.labels = labels;
    POLICIES.forEach((p, i) => { labLoss.data.datasets[i].data = ma(data[p.id].loss); labTier.data.datasets[i].data = data[p.id].tier; });
    labLoss.update("none"); labTier.update("none"); }
  updateSummary();
}

/* ---------- scoreboard, heatmap, log ---------- */
function drawScore() {
  $("score").querySelector("tbody").innerHTML = POLICIES.map((p) => {
    const d = data[p.id], n = d.n || 1;
    const avg = d.loss.reduce((a, b) => a + b, 0) / n;
    return `<tr><td><span class="dot" style="background:${p.color}"></span>${p.label.split(" (")[0]}</td>
      <td>${d.n ? avg.toFixed(2) : "–"}</td><td>${d.n ? Math.round(d.lat / n) : "–"}</td>
      <td>${d.n ? d.cost.toFixed(1) : "–"}</td><td>${d.viol}</td><td>${d.sw}</td></tr>`;
  }).join("");
  $("labScore").innerHTML = `<div class="lab-score-grid">${POLICIES.map((p) => { const d=data[p.id], avg=d.n?d.loss.reduce((a,b)=>a+b,0)/d.n:0; return `<div class="score-block"><h3><span class="dot" style="background:${p.color}"></span>${p.label.split(" (")[0]}</h3><div class="score-line"><span>Average loss</span><i class="score-track"><i style="width:${d.n?Math.min(100,avg/4*100):0}%;background:${p.color}"></i></i><b>${d.n?avg.toFixed(2):"—"}</b></div><div class="score-line"><span>Average latency</span><i class="score-track"><i style="width:${d.n?Math.min(100,d.lat/d.n/15):0}%;background:${p.color}"></i></i><b>${d.n?Math.round(d.lat/d.n)+" ms":"—"}</b></div><div class="score-line"><span>Total cost</span><i class="score-track"><i style="width:${d.n?Math.min(100,d.cost/10):0}%;background:${p.color}"></i></i><b>${d.n?d.cost.toFixed(1)+" µ$":"—"}</b></div><div class="score-line"><span>SLO misses</span><i class="score-track"><i style="width:${d.n?Math.min(100,d.viol/Math.max(1,d.n)*100):0}%;background:${p.color}"></i></i><b>${d.viol}</b></div></div>`;}).join("")}</div>`;
}

function updateSummary() {
  if (!data || !data.aamf) return;
  const a=data.aamf, base=data.static, count=Object.values(data).reduce((n,d)=>n+d.loss.length,0);
  $("metricMemory").textContent=""+a.n;
  $("metricSlo").textContent=""+a.viol;
  $("metricLoss").textContent=a.n&&base.n?`${Math.max(0,Math.round((1-(a.loss.reduce((x,y)=>x+y,0)/a.n)/(base.loss.reduce((x,y)=>x+y,0)/base.n))*100))}%`:"—";
  $("metricForget").textContent="";
  const counts={exploit:0,transfer:0,explore:0}; document.querySelectorAll("#log .tag").forEach(t=>{if(counts[t.textContent]!==undefined)counts[t.textContent]++});
  $("exploitCount").textContent=counts.exploit; $("transferCount").textContent=counts.transfer; $("exploreCount").textContent=counts.explore;
  if (count===0) $("metricMemory").textContent="0";
}

async function refreshMemory() {
  if (!connected) return;
  const m = await api("/api/memory");
  const rows = {};
  m.policy.forEach((r) => (rows[`${r.ctx}|${r.config}`] = r));
  let html = "<table><thead><tr><th>workload</th>" + m.tiers.map((t) => `<th>${t} MB</th>`).join("") + "</tr></thead><tbody>";
  for (let w = m.work_levels[0]; w <= m.work_levels[1]; w++) {
    const cells = m.tiers.map((t) => rows[`${w}|${t}`]);
    const seen = cells.filter((c) => c && c.n > 0);
    const best = seen.length ? seen.reduce((a, b) => (a.ema_reward > b.ema_reward ? a : b)).config : null;
    html += `<tr><th>level ${w}</th>` + cells.map((c, i) => {
      if (!c || c.n <= 0) return `<td class="empty">·</td>`;
      const loss = -c.ema_reward, g = Math.max(0, Math.min(1, 1 - loss / 4)); // green good, red bad
      return `<td class="${c.config === best ? "best" : ""}" style="background:hsl(${g * 125},55%,${16 + g * 8}%)">
        ${c.config === best ? "★ " : ""}${loss.toFixed(2)}<small>n=${c.n}</small></td>`;
    }).join("") + "</tr>";
  }
  $("heat").innerHTML = html + "</tbody></table>";
  $("labHeat").innerHTML = $("heat").innerHTML;
  $("memoryFull").innerHTML = $("heat").innerHTML;
  const learnedCells = new Set(m.policy.filter(r => r.n > 0).map(r => `${r.ctx}|${r.config}`));
  updateSummary();
  $("metricMemory").textContent = "" + learnedCells.size;
}

function logDecision(r) {
  const li = document.createElement("li");
  const forget = r.learned && r.learned.forgot ? `<span class="tag forget">forgot (surprise ${r.learned.surprise})</span>` : "";
  const pr = r.info.pruned && r.info.pruned.length ? ` · pruned ${r.info.pruned.join("/")} MB (SLO dominance)` : "";
  li.innerHTML = `<span class="tag ${r.info.source}">${r.info.source}</span>${forget}
    <b>workload ${r.work} → ${r.tier} MB</b> · ${Math.round(r.latency_ms)} ms · ${r.cost_ud.toFixed(2)} µ$ · loss ${r.loss.toFixed(2)}
    <br>${r.info.reason}${pr}`;
  const ul = $("log"); ul.prepend(li.cloneNode(true)); while (ul.children.length > 14) ul.lastChild.remove();
  const lab = $("labLog"); lab.prepend(li); while (lab.children.length > 14) lab.lastChild.remove();
  updateSummary();
}

/* ---------- run loop ---------- */
async function run() {
  running = true; resetData(); $("log").innerHTML = "";
  $("btnRun").disabled = true; $("btnReset").disabled = true; $("btnStop").disabled = false;
  const n = parseInt($("steps").value, 10); plan = makePlan(n);
  try {
    for (let i = 0; i < n && running; i++) {
      const extra = $("drift").checked ? 400 : 0;
      const res = await Promise.all(POLICIES.map((p) => api("/api/step", { policy: p.id, work: plan[i], extra_ms: extra })));
      res.forEach((r) => {
        const d = data[r.policy];
        d.loss.push(r.loss); d.tier.push(r.tier); d.lat += r.latency_ms; d.cost += r.cost_ud; d.n++;
        if (r.violation) d.viol++;
        if (d.last !== null && d.last !== r.tier) d.sw++;
        d.last = r.tier;
        if (r.policy === "aamf") logDecision(r);
      });
      drawCharts(); drawScore();
      $("progress").textContent = `Request ${i + 1} / ${n} · workload level ${plan[i]}${extra ? " · +400 ms downstream slowdown" : ""}`;
      if (i % 3 === 2 || i === n - 1) await refreshMemory();
    }
    $("progress").textContent += running ? " · done" : " · stopped";
  } catch (e) { toast(e.message); $("progress").className = "status err"; $("progress").textContent = e.message; }
  running = false; $("btnStop").disabled = true;
  showConn(await api("/api/status")); await refreshMemory().catch(() => {});
}

async function wipe() {
  await api("/api/reset", {}); resetData(); drawCharts(); drawScore();
  $("log").innerHTML = ""; $("progress").textContent = "Memory wiped. AAMF is cold again.";
  await refreshMemory();
}

/* ---------- init ---------- */
window.addEventListener("DOMContentLoaded", async () => {
  cLoss = lineChart($("cLoss"), { y: { beginAtZero: true } });
  cTier = lineChart($("cTier"), { stepped: true, y: { type: "logarithmic", min: 100, max: 2500,
    afterBuildTicks: (a) => { a.ticks = [128, 256, 512, 1024, 2048].map((value) => ({ value })); } } });
  labLoss = lineChart($("labLoss"), { y: { beginAtZero: true } });
  labTier = lineChart($("labTier"), { stepped: true, y: { type: "logarithmic", min: 100, max: 2500, afterBuildTicks: (a) => { a.ticks = [128, 256, 512, 1024, 2048].map((value) => ({ value })); } } });
  resetData(); drawScore(); syncMode();
  $("mode").addEventListener("change", syncMode);
  $("btnConnect").addEventListener("click", connect);
  $("btnProvision").addEventListener("click", provisionResources);
  $("btnStopSession").addEventListener("click", () => disconnect().catch((e) => toast(e.message)));
  $("refreshFleet").addEventListener("click", () => loadAwsView("fleet"));
  $("refreshCloudwatch").addEventListener("click", () => loadAwsView("cloudwatch"));
  $("refreshTrail").addEventListener("click", () => loadAwsView("cloudtrail"));
  $("refreshConfig").addEventListener("click", () => loadAwsView("config"));
  document.querySelectorAll("[data-view]").forEach(el => el.addEventListener("click", ev => { ev.preventDefault(); const name=el.dataset.view; document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active",v.id===`view-${name}`)); document.querySelectorAll(".nav-link").forEach(a=>a.classList.toggle("active",a.dataset.view===name)); $("pageCrumb").textContent=el.textContent.trim().replace(/[↗→]/g,"").trim(); history.replaceState(null,"",`#${name}`); loadAwsView(name); }));
  $("themeToggle").addEventListener("click", () => { document.body.classList.toggle("dark"); $("themeToggle").querySelector("small").textContent=document.body.classList.contains("dark")?"Dark interface":"Light interface"; [cLoss,cTier,labLoss,labTier].filter(Boolean).forEach(c=>{c.options.scales.x.ticks.color=document.body.classList.contains("dark")?"#a5b4c8":"#8a96a7";c.options.scales.y.ticks.color=document.body.classList.contains("dark")?"#a5b4c8":"#8a96a7";c.options.scales.x.grid.color=document.body.classList.contains("dark")?"#2b3b51":"#e8edf3";c.options.scales.y.grid.color=document.body.classList.contains("dark")?"#2b3b51":"#e8edf3";c.update("none")}); });
  $("btnDisconnect").addEventListener("click", disconnect);
  $("btnRun").addEventListener("click", run);
  $("btnStop").addEventListener("click", () => (running = false));
  $("btnReset").addEventListener("click", () => wipe().catch((e) => toast(e.message)));
  const initialView=location.hash.slice(1); if(initialView && $("view-"+initialView)){document.querySelector(`[data-view="${initialView}"]`)?.click();}
  try { const s = await api("/api/status"); showConn(s); if (s.connected) await refreshMemory(); await loadVisibleAwsView(); } catch (_) {}
});
