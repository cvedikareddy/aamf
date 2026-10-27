"""AAMF backend: serves the website and a tiny JSON API.

    python backend/app.py        ->  http://127.0.0.1:8000
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory, session  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

import config as C  # noqa: E402
from aws_session import STORE, ConnectError  # noqa: E402
from engine import loss_of  # noqa: E402
from invoker import invoke  # noqa: E402

INFRA = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra"))
sys.path.insert(0, INFRA)

FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET") or os.urandom(24).hex()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def current():
    s = STORE.get(session.get("sid"))
    if not s:
        raise ConnectError("Not connected. Open a session first.", 401)
    return s


@app.errorhandler(ConnectError)
def _connect_error(e):
    return jsonify(error=str(e)), e.status


@app.errorhandler(Exception)
def _any_error(e):
    if isinstance(e, HTTPException):
        return e
    return jsonify(error=f"{type(e).__name__}: {e}"), 500


# ---- static site --------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/<path:name>")
def assets(name):
    return send_from_directory(FRONTEND, name)


# ---- session ------------------------------------------------------------------------
@app.post("/api/connect")
def connect():
    b = request.get_json(force=True) or {}
    old = session.get("sid")
    s = STORE.open(mode=b.get("mode", "demo"), region=b.get("region"), profile=b.get("profile"),
                   access_key=b.get("access_key"), secret_key=b.get("secret_key"),
                   token=b.get("session_token"))
    if old and old != s.sid:
        STORE.close(old)
    session["sid"] = s.sid
    return jsonify(s.describe())


@app.post("/api/disconnect")
def disconnect():
    STORE.close(session.pop("sid", None))
    return jsonify(connected=False)


@app.post("/api/provision")
def provision():
    s = current()
    if s.demo or s.boto_session is None:
        raise ConnectError("Connect to an AWS profile or credentials before creating AAMF resources.", 400)
    if not s.missing:
        return jsonify(s.describe())
    from setup_aws import provision_session
    from botocore.exceptions import ClientError, NoCredentialsError
    try:
        provision_session(s.boto_session, s.region)
        return jsonify(s.refresh_resources())
    except NoCredentialsError:
        profile = getattr(s.boto_session, "profile_name", None) or "default"
        return jsonify(error=f"AWS credentials are unavailable or expired. Run aws login --profile {profile!r} in PowerShell, then reconnect AAMF."), 401
    except ClientError as e:
        error = e.response.get("Error", {})
        code = error.get("Code", "AWSServiceError")
        status_code = 403 if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation") else 502
        return jsonify(error=f"AWS setup failed ({code}). Check the AWS permissions required by AAMF."), status_code

@app.get("/api/aws/<service>")
def aws_service(service):
    s = current()
    if s.demo or s.boto_session is None:
        raise ConnectError("Connect with an AWS profile or credentials to load live service data.", 400)
    from aws_services import cloudtrail_events, cloudwatch, config_status, ec2_fleet, resources
    from botocore.exceptions import ClientError, NoCredentialsError
    handlers = {"resources": resources, "ec2": ec2_fleet, "cloudwatch": cloudwatch,
                "cloudtrail": cloudtrail_events, "config": config_status}
    handler = handlers.get(service)
    if handler is None:
        return jsonify(error="Unknown AWS service view."), 404
    try:
        return jsonify(handler(s.boto_session))
    except NoCredentialsError:
        profile = getattr(s.boto_session, "profile_name", None) or "default"
        return jsonify(error=f"AWS credentials are unavailable or expired. Run aws login --profile {profile!r} in PowerShell, then reconnect AAMF."), 401
    except ClientError as e:
        error = e.response.get("Error", {})
        code = error.get("Code", "AWSServiceError")
        status_code = 403 if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation") else 502
        return jsonify(error=f"{service} read failed ({code}): {error.get('Message', 'AWS request failed')}"), status_code
@app.get("/api/status")
def status():
    s = STORE.get(session.get("sid"))
    return jsonify(s.describe() if s else {"connected": False})


# ---- one decision cycle: decide -> invoke -> learn -> log ---------------------------
@app.post("/api/step")
def step():
    s = current()
    if not s.demo and s.missing:
        raise ConnectError("AWS resources missing: run infra/setup_aws.py first (" +
                           ", ".join(s.missing[:3]) + "...)", 409)
    b = request.get_json(force=True)
    pol_id = b["policy"]
    work = max(C.WORK_MIN, min(int(b["work"]), C.WORK_MAX))
    extra = max(0, min(int(b.get("extra_ms", 0)), 2000))
    pol = s.policies[pol_id]

    tier, info = pol.decide(work)
    res = invoke(s, tier, work, extra)
    learned = pol.learn(work, tier, res)
    loss = loss_of(res["latency_ms"], res["cost_ud"])

    s.memory.log_episode({
        "policy": pol_id, "ctx": work, "config": tier, "source": info["source"],
        "latency_ms": res["latency_ms"], "cost_ud": res["cost_ud"], "loss": loss,
        "extra_ms": extra, "forgot": bool(learned.get("forgot", False)),
    })
    return jsonify({
        "policy": pol_id, "work": work, "tier": tier, "extra_ms": extra,
        "latency_ms": res["latency_ms"], "billed_ms": res["billed_ms"],
        "cost_ud": res["cost_ud"], "cold_ms": res["cold_ms"], "loss": loss,
        "violation": res["latency_ms"] > C.SLO_MS,
        "info": info, "learned": learned,
    })


# ---- memory inspection / reset ------------------------------------------------------
@app.get("/api/memory")
def memory():
    s = current()
    return jsonify({"policy": s.memory.snapshot(), "episodes": s.memory.recent(15),
                    "tiers": C.TIERS, "work_levels": [C.WORK_MIN, C.WORK_MAX], "slo_ms": C.SLO_MS})


@app.post("/api/reset")
def reset():
    s = current()
    s.memory.reset()
    s.reset_policies()
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"AAMF running on http://127.0.0.1:{port}  (localhost only)")
    app.run(host="127.0.0.1", port=port, threaded=True)
