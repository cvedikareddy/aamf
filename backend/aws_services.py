"""Read-only, region-scoped AWS inventory for AAMF."""
from datetime import datetime, timedelta, timezone
import config as C


def _functions(session):
    rows = []
    pages = session.client("lambda").get_paginator("list_functions").paginate()
    for page in pages:
        rows.extend(f for f in page.get("Functions", []) if f.get("FunctionName", "").startswith(f"{C.PREFIX}-target-"))
    return rows


def resources(session):
    ddb = session.client("dynamodb")
    names, token = [], None
    while True:
        page = ddb.list_tables(**({"ExclusiveStartTableName": token} if token else {}))
        names.extend(n for n in page.get("TableNames", []) if n.startswith(f"{C.PREFIX}-"))
        token = page.get("LastEvaluatedTableName")
        if not token: break
    tables = []
    for name in names:
        t = ddb.describe_table(TableName=name)["Table"]
        tables.append({"name": name, "status": t.get("TableStatus"), "item_count": t.get("ItemCount", 0),
                       "billing_mode": t.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")})
    lambdas = [{"name": f.get("FunctionName"), "runtime": f.get("Runtime"), "memory_mb": f.get("MemorySize"),
                "timeout_s": f.get("Timeout"), "state": f.get("State", "Active"), "last_modified": f.get("LastModified")}
               for f in _functions(session)]
    return {"region": session.region_name or C.DEFAULT_REGION, "lambdas": lambdas, "tables": tables}


def ec2_fleet(session):
    instances = []
    paginator = session.client("ec2").get_paginator("describe_instances")
    pages = paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    for page in pages:
        for reservation in page.get("Reservations", []):
            for i in reservation.get("Instances", []):
                tags = {t.get("Key"): t.get("Value") for t in i.get("Tags", [])}
                instances.append({"id": i.get("InstanceId"), "name": tags.get("Name", ""), "type": i.get("InstanceType"),
                                  "state": i.get("State", {}).get("Name"), "az": i.get("Placement", {}).get("AvailabilityZone"),
                                  "launch_time": i.get("LaunchTime").isoformat() if i.get("LaunchTime") else None})
    return {"region": session.region_name or C.DEFAULT_REGION, "instances": instances}


def cloudtrail_events(session):
    response = session.client("cloudtrail").lookup_events(MaxResults=50)
    events = [{"id": e.get("EventId"), "name": e.get("EventName"), "source": e.get("EventSource"),
               "time": e.get("EventTime").isoformat() if e.get("EventTime") else None, "user": e.get("Username"),
               "resources": [r.get("ResourceName") for r in e.get("Resources", []) if r.get("ResourceName")]}
              for e in response.get("Events", [])]
    return {"region": session.region_name or C.DEFAULT_REGION, "events": events}


def cloudwatch(session):
    cw = session.client("cloudwatch")
    alarms = cw.describe_alarms(AlarmNamePrefix=f"{C.PREFIX}-", MaxRecords=100)
    funcs = _functions(session)
    queries, labels = [], {}
    suffix = {"Invocations": "i", "Errors": "e", "Duration": "d"}
    for index, f in enumerate(funcs):
        name = f.get("FunctionName", "")
        for metric, stat in (("Invocations", "Sum"), ("Errors", "Sum"), ("Duration", "Average")):
            qid = f"m{index}{suffix[metric]}"
            labels[qid] = {"function": name, "metric": metric, "stat": stat}
            queries.append({"Id": qid, "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": metric,
                           "Dimensions": [{"Name": "FunctionName", "Value": name}]}, "Period": 60, "Stat": stat}, "ReturnData": True})
    points = []
    if queries:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        data = cw.get_metric_data(MetricDataQueries=queries, StartTime=end-timedelta(minutes=30), EndTime=end,
                                  ScanBy="TimestampAscending")
        for r in data.get("MetricDataResults", []):
            points.append({**labels.get(r.get("Id"), {}), "points": [{"time": t.isoformat(), "value": v}
                          for t, v in zip(r.get("Timestamps", []), r.get("Values", []))]})
    rows = [{"name": a.get("AlarmName"), "state": a.get("StateValue"), "reason": a.get("StateReason", ""),
             "metric": a.get("MetricName", "Composite alarm"), "namespace": a.get("Namespace", "")}
            for group in (alarms.get("MetricAlarms", []), alarms.get("CompositeAlarms", [])) for a in group]
    return {"region": session.region_name or C.DEFAULT_REGION, "window_minutes": 30, "metrics": points, "alarms": rows}


def config_status(session):
    c = session.client("config")
    recorders = c.describe_configuration_recorders().get("ConfigurationRecorders", [])
    statuses = {x.get("name"): x for x in c.describe_configuration_recorder_status().get("ConfigurationRecordersStatus", [])}
    rules = []
    for page in c.get_paginator("describe_config_rules").paginate():
        rules.extend(page.get("ConfigRules", []))
    compliance = []
    if rules and recorders:
        for page in c.get_paginator("describe_compliance_by_config_rule").paginate(
            ComplianceTypes=["COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE"]
        ):
            compliance.extend(page.get("ComplianceByConfigRules", []))
    comp = {x.get("ConfigRuleName"): x.get("Compliance", {}).get("ComplianceType", "NOT_EVALUATED") for x in compliance}
    return {"region": session.region_name or C.DEFAULT_REGION, "configured": bool(recorders),
            "recorders": [{"name": r.get("name"), "recording": statuses.get(r.get("name"), {}).get("recording", False),
                           "last_status": statuses.get(r.get("name"), {}).get("lastStatus")} for r in recorders],
            "rules": [{"name": r.get("ConfigRuleName"), "state": r.get("ConfigRuleState"),
                       "compliance": comp.get(r.get("ConfigRuleName"), "NOT_EVALUATED")} for r in rules]}
