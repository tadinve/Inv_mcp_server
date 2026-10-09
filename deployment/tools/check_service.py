"""Check a `gcloud run services describe --format=json` document against the deployment contract.

Usage: gcloud run services describe SERVICE --format=json | python3 check_service.py
Exits non-zero if any check fails. Prints configuration names and values that are not secret.
"""

import json
import sys


def checks(svc: dict) -> dict[str, bool]:
    template = svc["spec"]["template"]
    tpl_ann = template["metadata"].get("annotations", {})
    svc_ann = svc["metadata"].get("annotations", {})
    env = {e["name"]: e.get("value", "") for e in template["spec"]["containers"][0].get("env", [])}
    policy = json.loads(env.get("INVENTORY_POLICY_JSON") or '{"principals": []}')
    return {
        "invoker IAM check enabled": svc_ann.get("run.googleapis.com/invoker-iam-disabled", "false") != "true",
        "ingress all (IAM-protected)": svc_ann.get("run.googleapis.com/ingress", "all") == "all",
        "min instances 0": tpl_ann.get("autoscaling.knative.dev/minScale", "0") == "0",
        "max instances 1": tpl_ann.get("autoscaling.knative.dev/maxScale") == "1",
        "auth mode google": env.get("INVENTORY_AUTH_MODE") == "google",
        "audience is https": env.get("INVENTORY_OIDC_AUDIENCE", "").startswith("https://"),
        "allowed hosts set": bool(env.get("INVENTORY_ALLOWED_HOSTS")),
        "no dev override": "INVENTORY_DEV_ALLOW_NON_LOOPBACK" not in env,
        "policy has exactly 2 subjects": len(policy.get("principals", [])) == 2,
        "label data=synthetic": svc["metadata"].get("labels", {}).get("data") == "synthetic",
    }


def main() -> int:
    svc = json.load(sys.stdin)
    results = checks(svc)
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    env = {e["name"]: e.get("value", "") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    print(f"  audience={env.get('INVENTORY_OIDC_AUDIENCE')}  allowed_hosts={env.get('INVENTORY_ALLOWED_HOSTS')}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
