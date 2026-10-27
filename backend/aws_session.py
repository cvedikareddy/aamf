"""Opening, validating and tracking AWS sessions from the website.

The browser never sees credentials after submitting them. It only holds an opaque
cookie (Flask signed session -> sid). The boto3 Session lives in server memory and
expires after SESSION_TTL_S.  Run this on localhost only.
"""
import secrets
import threading
import time

import config as C
from engine import AAMF, StaticTier, ThresholdRule
from memory import DynamoMemory, LocalMemory

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:
    boto3 = None


class ConnectError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class AppSession:
    def __init__(self, sid, mode, region, boto_session=None, identity=None, missing=None):
        self.sid, self.mode, self.region = sid, mode, region
        self.demo = boto_session is None
        self.identity = identity or {}
        self.missing = missing or []
        self.expires = time.time() + C.SESSION_TTL_S
        self.boto_session = boto_session
        self._clients = {}
        if self.demo:
            self.memory, self.lambda_client = LocalMemory(), None
        else:
            self.memory = self._memory_for_resources(self.missing)
            self.lambda_client = self.client("lambda")
        self.reset_policies()

    def _memory_for_resources(self, missing):
        missing_tables = {C.TABLE_POLICY, C.TABLE_EPISODES}.intersection(missing)
        if missing_tables:
            memory = LocalMemory()
            memory.kind = "in-process (AWS setup pending)"
            return memory
        return DynamoMemory(self.boto_session)

    def refresh_resources(self):
        self.missing = _check_resources(self.boto_session)
        self.memory = self._memory_for_resources(self.missing)
        self.reset_policies()
        return self.describe()

    def client(self, service):
        if self.boto_session is None:
            return None
        if service not in self._clients:
            self._clients[service] = self.boto_session.client(
                service, config=Config(read_timeout=60, retries={"max_attempts": 2}))
        return self._clients[service]

    def reset_policies(self):
        self.policies = {"aamf": AAMF(self.memory), "threshold": ThresholdRule(),
                         "static": StaticTier()}

    def describe(self):
        return {
            "connected": True, "mode": self.mode, "region": self.region,
            "account": self.identity.get("Account"), "arn": self.identity.get("Arn"),
            "ready": not self.missing, "missing": self.missing,
            "memory": self.memory.kind,
            "expires_in": max(0, int(self.expires - time.time())),
        }


def _check_resources(sess):
    lam, ddb = sess.client("lambda"), sess.client("dynamodb")
    missing = []
    for t in C.TIERS:
        try:
            lam.get_function(FunctionName=C.fn_name(t))
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            missing.append(C.fn_name(t))
    for t in (C.TABLE_POLICY, C.TABLE_EPISODES):
        try:
            ddb.describe_table(TableName=t)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            missing.append(t)
    return missing


class Store:
    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def open(self, mode="demo", region=None, profile=None,
             access_key=None, secret_key=None, token=None) -> AppSession:
        region = region or C.DEFAULT_REGION
        self._sweep()
        sid = secrets.token_urlsafe(24)

        if mode == "demo":
            s = AppSession(sid, "demo", "local")
        else:
            if boto3 is None:
                raise ConnectError(r"AWS SDK is unavailable in the Python environment serving AAMF. Install with .venv\Scripts\python.exe -m pip install -r backend\requirements.txt, then start .venv\Scripts\python.exe backend\app.py.")
            try:
                if mode == "keys":
                    if not (access_key and secret_key):
                        raise ConnectError("Access key and secret are required")
                    bs = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                       aws_session_token=token or None, region_name=region)
                elif mode == "profile":
                    bs = boto3.Session(profile_name=profile or None, region_name=region)
                else:  # "default": env vars, SSO, instance role... whatever boto3 resolves
                    bs = boto3.Session(region_name=region)
                ident = bs.client("sts").get_caller_identity()
                missing = _check_resources(bs)
            except ConnectError:
                raise
            except NoCredentialsError as e:
                if mode == "profile":
                    selected = profile or "default"
                    remedy = f"Run aws login --profile {selected!r} in PowerShell, then reconnect AAMF."
                elif mode == "keys":
                    remedy = "Re-enter the AWS access key and secret for this session."
                else:
                    remedy = "Run aws login in PowerShell, then reconnect AAMF."
                raise ConnectError(f"AWS credentials are unavailable or expired. {remedy}", 401) from e
            except (BotoCoreError, ClientError) as e:
                raise ConnectError(f"AWS rejected the session: {e}", 401)
            s = AppSession(sid, mode, region, bs, ident, missing)

        with self._lock:
            self._d[sid] = s
        return s

    def get(self, sid):
        with self._lock:
            s = self._d.get(sid)
            if s and s.expires < time.time():
                self._d.pop(sid, None)
                return None
            return s

    def close(self, sid):
        with self._lock:
            self._d.pop(sid, None)

    def _sweep(self):
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._d.items() if v.expires < now]:
                self._d.pop(k, None)


STORE = Store()
