"""Summarize `gcloud logging read --format=json` output for the MCP server.

Usage: gcloud logging read FILTER --format=json | python3 summarize_logs.py [--require-tool-calls] [--require-denial]

Prints counts per (event, tool, outcome, authorization, category) for the application's
structured entries, then the number of entries of ANY kind that look like they contain a
JWT. Fails if any do, or if a --require-* condition is not met (an empty log is not evidence).
"""

import collections
import json
import re
import sys

JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")


def main(argv: list[str]) -> int:
    entries = json.load(sys.stdin)
    rows: collections.Counter = collections.Counter()
    for entry in entries:
        p = entry.get("jsonPayload", {})
        if "event" not in p:
            continue  # platform request logs etc.: leak-checked below, not summarized
        category = p.get("error_category") or p.get("reason") or "-"
        rows[
            (p.get("event", "-"), p.get("tool", "-"), p.get("outcome", "-"), p.get("authorization", "-"), category)
        ] += 1
    print(f"  {'event':<15}{'tool':<24}{'outcome':<19}{'authz':<7}{'category':<22}count")
    for key, count in sorted(rows.items()):
        print("  " + "".join(f"{v!s:<{w}}" for v, w in zip(key, (15, 24, 19, 7, 22), strict=True)) + str(count))
    leaks = sum(1 for entry in entries if JWT.search(json.dumps(entry)))
    print(f"  entries scanned: {len(entries)}; containing a JWT-like string: {leaks} (expected 0)")
    failures = []
    if leaks:
        failures.append("token-like strings found in logs")
    tool_calls = sum(n for key, n in rows.items() if key[0] == "tool_call")
    if "--require-tool-calls" in argv and tool_calls == 0:
        failures.append("no tool_call entries found (logs missing or not yet ingested)")
    denials = sum(n for key, n in rows.items() if key[0] == "tool_call" and key[3] == "deny")
    if "--require-denial" in argv and denials == 0:
        failures.append("no authorization denial logged (the forbidden-write test should produce one)")
    for failure in failures:
        print(f"  [FAIL] {failure}")
    if not failures:
        print("  [PASS] log verification")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
