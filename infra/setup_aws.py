"""One-shot provisioning for AAMF.

Creates: 1 IAM role, 5 Lambda functions (128..2048 MB), 2 DynamoDB tables.
Safe to re-run (idempotent).

    python infra/setup_aws.py --profile myprofile --region ap-south-1
"""
import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import config as C  # noqa: E402

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(HERE, "..", "target", "handler.py"), "handler.py")
    return buf.getvalue()


def ensure_role(iam) -> str:
    try:
        arn = iam.get_role(RoleName=C.ROLE_NAME)["Role"]["Arn"]
        print(f"role     exists   {C.ROLE_NAME}")
        return arn
    except iam.exceptions.NoSuchEntityException:
        pass
    arn = iam.create_role(
        RoleName=C.ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(TRUST),
        Description="Execution role for AAMF demo Lambdas",
    )["Role"]["Arn"]
    iam.attach_role_policy(RoleName=C.ROLE_NAME, PolicyArn=BASIC_EXEC)
    print(f"role     created  {C.ROLE_NAME} (waiting 12s for IAM propagation)")
    time.sleep(12)
    return arn


def ensure_function(lam, tier: int, role_arn: str, code: bytes) -> None:
    name = C.fn_name(tier)
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        print(f"lambda   updated  {name}")
        return
    except lam.exceptions.ResourceNotFoundException:
        pass
    for attempt in range(8):
        try:
            lam.create_function(
                FunctionName=name, Runtime="python3.12", Role=role_arn,
                Handler="handler.handler", Code={"ZipFile": code},
                Timeout=30, MemorySize=tier, Publish=True,
                Description=f"AAMF target workload @ {tier} MB",
            )
            break
        except ClientError as e:
            # a brand-new role is sometimes not assumable for a few seconds
            if e.response["Error"]["Code"] == "InvalidParameterValueException" and attempt < 7:
                time.sleep(5)
                continue
            raise
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    print(f"lambda   created  {name}")


def ensure_table(ddb, name, hash_key, hash_type, range_key, range_type) -> None:
    try:
        ddb.describe_table(TableName=name)
        print(f"dynamodb exists   {name}")
        return
    except ddb.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": hash_key, "KeyType": "HASH"},
                   {"AttributeName": range_key, "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": hash_key, "AttributeType": hash_type},
                              {"AttributeName": range_key, "AttributeType": range_type}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.get_waiter("table_exists").wait(TableName=name)
    print(f"dynamodb created  {name}")


def provision_session(s, region=None) -> dict:
    """Create or update all AAMF resources using an existing boto3 session."""
    region = region or s.region_name or C.DEFAULT_REGION
    ident = s.client("sts").get_caller_identity()
    print(f"account {ident['Account']}  region {region}\n")

    role_arn = ensure_role(s.client("iam"))
    lam, code = s.client("lambda"), build_zip()
    for tier in C.TIERS:
        ensure_function(lam, tier, role_arn, code)

    ddb = s.client("dynamodb")
    ensure_table(ddb, C.TABLE_POLICY, "ctx", "N", "config", "N")
    ensure_table(ddb, C.TABLE_EPISODES, "stream", "S", "ts", "S")
    try:
        ddb.update_time_to_live(
            TableName=C.TABLE_EPISODES,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires"})
    except ClientError as e:  # already enabled / being enabled
        print(f"ttl      note     {e.response['Error']['Message']}")

    print("\nDone. AAMF resources are ready.")
    return ident


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None)
    ap.add_argument("--region", default=C.DEFAULT_REGION)
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    provision_session(s, a.region)

if __name__ == "__main__":
    main()
