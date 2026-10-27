"""Delete everything setup_aws.py created, so nothing keeps costing money.

    python infra/teardown_aws.py --profile myprofile --region ap-south-1
"""
import argparse
import os
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import config as C  # noqa: E402

BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None)
    ap.add_argument("--region", default=C.DEFAULT_REGION)
    a = ap.parse_args()
    profile = a.profile or "default credential chain"
    print(f"Starting teardown (profile: {profile}, region: {a.region})", flush=True)
    s = boto3.Session(profile_name=a.profile, region_name=a.region)

    lam, ddb, iam = s.client("lambda"), s.client("dynamodb"), s.client("iam")
    deleted = 0

    for tier in C.TIERS:
        name = C.fn_name(tier)
        print(f"Checking Lambda {name}...", flush=True)
        try:
            lam.delete_function(FunctionName=name)
            print(f"deleted lambda  {name}", flush=True)
            deleted += 1
        except lam.exceptions.ResourceNotFoundException:
            print(f"not found       Lambda {name}", flush=True)

    for t in (C.TABLE_POLICY, C.TABLE_EPISODES):
        print(f"Checking DynamoDB table {t}...", flush=True)
        try:
            ddb.delete_table(TableName=t)
            print(f"deleted dynamodb {t}", flush=True)
            deleted += 1
        except ddb.exceptions.ResourceNotFoundException:
            print(f"not found       DynamoDB table {t}", flush=True)

    print(f"Checking IAM role {C.ROLE_NAME}...", flush=True)
    try:
        iam.detach_role_policy(RoleName=C.ROLE_NAME, PolicyArn=BASIC_EXEC)
        iam.delete_role(RoleName=C.ROLE_NAME)
        print(f"deleted role    {C.ROLE_NAME}", flush=True)
        deleted += 1
    except iam.exceptions.NoSuchEntityException:
        print(f"not found       IAM role {C.ROLE_NAME}", flush=True)

    print(f"Teardown complete: {deleted} resource(s) deleted.", flush=True)


if __name__ == "__main__":
    main()
