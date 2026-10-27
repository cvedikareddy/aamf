"""AAMF Flask site and JSON API."""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

import config as C
from aws_session import STORE, ConnectError
from engine import default_context, loss_of, normalize_context
from invoker import invoke

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
def _connect_error(error):
    return jsonify(error=str(error)), error.status


@app.errorhandler(Exception)
def _any_error(error):
    if isinstance(error, HTTPException):
        return error
    return jsonify(error=f"{type(error).__name__}: {error}"), 500


@app.get("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.get("/<path:name>")
def assets(name):
    return send_from_directory(FRONTEND, name)


@app.post("/api/connect")
def connect():
    body = request.get_json(force=True) or {}
    previous = session.get("sid")
    app_session = STORE.open(mode=body.get("mode", "demo"), region=body.get("region"),
                             profile=body.get("profile"), access_key=body.get("access_key"),
                             secret_key=body.get("secret_key"), token=body.get("session_token"))
    if previous and previous != app_session.sid:
        STORE.close(previous)
    session["sid"] = app_session.sid
    return jsonify(app_session.describe())


@app.post("/api/disconnect")
def disconnect():
    STORE.close(session.pop("sid", None))
    return jsonify(connected=False)


@app.post("/api/provision")
def provision():
    app_session = current()
    if app_session.demo or app_session.boto_session is None:
        raise ConnectError("Connect to an AWS profile or credentials before creating AAMF resources.", 400)
    if not app_session.missing:
        return jsonify(app_session.describe())
    from setup_aws import provision_session
    from botocore.exceptions import ClientError, NoCredentialsError
    try:
        provision_session(app_session.boto_session, app_session.region)
        return jsonify(app_session.refresh_resources())
    except NoCredentialsError:
        profile = getattr(app_session.boto_session, "profile_name", None) or "default"
        return jsonify(error=f"AWS credentials are unavailable or expired. Run aws login --profile {profile!r}, then reconnect AAMF."), 401
    except ClientError as error:
        detail = error.response.get("Error", {})
        code = detail.get("Code", "AWSServiceError")
        status = 403 if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation") else 502
        return jsonify(error=f"AWS setup failed ({code}). Check the AWS permissions required by AAMF."), status


@app.get("/api/aws/<service>")
def aws_service(service):
    app_session = current()
    if app_session.demo or app_session.boto_session is None:
        raise ConnectError("Connect with an AWS profile or credentials to load live service data.", 400)
    from aws_services import cloudtrail_events, cloudwatch, config_status, ec2_fleet, resources
    from botocore.exceptions import ClientError, NoCredentialsError
    handlers = {"resources": resources, "ec2": ec2_fleet, "cloudwatch": cloudwatch,
                "cloudtrail": cloudtrail_events, "config": config_status}
    handler = handlers.get(service)
    if handler is None:
        return jsonify(error="Unknown AWS service view."), 404
    try:
        return jsonify(handler(app_session.boto_session))
    except NoCredentialsError:
        profile = getattr(app_session.boto_session, "profile_name", None) or "default"
        return jsonify(error=f"AWS credentials are unavailable or expired. Run aws login --profile {profile!r}, then reconnect AAMF."), 401
    except ClientError as error:
        detail = error.response.get("Error", {})
        code = detail.get("Code", "AWSServiceError")
        status = 403 if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation") else 502
        return jsonify(error=f"{service} read failed ({code}): {detail.get('Message', 'AWS request failed')}"), status


@app.get("/api/status")
def status():
    app_session = STORE.get(session.get("sid"))
    return jsonify(app_session.describe() if app_session else {"connected": False})


@app.post("/api/step")
def step():
    app_session = current()
    if not app_session.demo and app_session.missing:
        raise ConnectError("AWS resources missing: run Setup to create required AAMF resources.", 409)
    body = request.get_json(force=True) or {}
    policy_id = body.get("policy")
    if policy_id not in app_session.policies:
        raise ConnectError("Choose a valid controller.", 400)
    try:
        work = max(C.WORK_MIN, min(int(body.get("work", C.WORK_MIN)), C.WORK_MAX))
        extra_ms = max(0, min(int(body.get("extra_ms", 0)), 2000))
        cost_weight = max(0.0, min(float(body.get("cost_weight", C.COST_W)), 1.0))
        context = normalize_context(body.get("context") or default_context(work), work)
    except (TypeError, ValueError) as error:
        raise ConnectError(f"Invalid workload input: {error}", 400) from error

    policy = app_session.policies[policy_id]
    tier, info = policy.decide(work, context=context, cost_weight=cost_weight, extra_ms=extra_ms)
    result = invoke(app_session, tier, work, extra_ms, context)
    learned = policy.learn(work, tier, result, context=context, cost_weight=cost_weight)
    loss = loss_of(result["latency_ms"], result["cost_ud"], cost_weight)
    audit_id = uuid.uuid4().hex
    episode = {
        "audit_id": audit_id, "policy": policy_id, "ctx": work, "config": tier,
        "context_features": context, "cost_weight": cost_weight, "source": info["source"],
        "decision_reason": info.get("reason", ""), "decision_evidence": info,
        "learning_result": learned, "latency_ms": result["latency_ms"],
        "billed_ms": result["billed_ms"], "cost_ud": result["cost_ud"],
        "loss": loss, "extra_ms": extra_ms, "forgot": bool(learned.get("forgot", False)),
    }
    app_session.memory.log_episode(episode)
    return jsonify({
        "audit_id": audit_id, "policy": policy_id, "work": work, "context": context,
        "tier": tier, "extra_ms": extra_ms, "cost_weight": cost_weight,
        "latency_ms": result["latency_ms"], "billed_ms": result["billed_ms"],
        "cost_ud": result["cost_ud"], "cold_ms": result["cold_ms"], "loss": loss,
        "violation": result["latency_ms"] > C.SLO_MS, "info": info, "learned": learned,
    })


@app.get("/api/memory")
def memory():
    app_session = current()
    return jsonify({"policy": app_session.memory.snapshot(), "episodes": app_session.memory.recent(80),
                    "tiers": C.TIERS, "work_levels": [C.WORK_MIN, C.WORK_MAX],
                    "context_features": list(C.CONTEXT_FEATURES), "slo_ms": C.SLO_MS,
                    "cost_ref_ud": C.COST_REF_UD})


@app.post("/api/reset")
def reset():
    app_session = current()
    app_session.memory.reset()
    app_session.reset_policies()
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"AAMF running on http://127.0.0.1:{port}  (localhost only)")
    app.run(host="127.0.0.1", port=port, threaded=True)
