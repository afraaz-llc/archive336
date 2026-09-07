"""Add an AbortIncompleteMultipartUpload rule, preserving what's there.

put_bucket_lifecycle_configuration REPLACES the whole config, so the
existing expire-noncurrent-versions rule has to be sent back with it.
"""
import json, sys
from app import r2

c, bucket = r2.client(), r2._bucket
existing = c.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
print("existing rules:", [r.get("ID") for r in existing])

RULE_ID = "abort-incomplete-multipart"
if any(r.get("ID") == RULE_ID for r in existing):
    print("rule already present; nothing to do")
    sys.exit(0)

new_rule = {
    "ID": RULE_ID,
    "Filter": {"Prefix": ""},
    "Status": "Enabled",
    # 7 days. The worker aborts its own failures, so this only catches
    # uploads whose worker died without getting the chance - a crash,
    # a kill, a machine losing power mid-upload. No real upload runs
    # for a week, so nothing legitimate is at risk.
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
}
rules = existing + [new_rule]

if "--apply" not in sys.argv:
    print("would write:")
    print(json.dumps(rules, indent=1, default=str))
    print("\ndry run - pass --apply")
    sys.exit(0)

c.put_bucket_lifecycle_configuration(
    Bucket=bucket, LifecycleConfiguration={"Rules": rules}
)
after = c.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
print("rules now:", [r.get("ID") for r in after])
