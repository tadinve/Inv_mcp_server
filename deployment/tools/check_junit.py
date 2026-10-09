"""Gate the real-identity integration run on its JUnit XML report.

Usage: python3 check_junit.py REPORT.xml REQUIRED_TESTS.txt

Fails unless: at least one test was collected; no test failed, errored or was skipped;
and every required test function ran (parametrized cases count under their base name).
"""

import sys
import xml.etree.ElementTree as ET  # parses our own pytest report, not untrusted input
from collections import defaultdict


def main(report: str, required_file: str) -> int:
    required = [line.strip() for line in open(required_file) if line.strip() and not line.startswith("#")]
    try:
        root = ET.parse(report).getroot()  # noqa: S314
    except (OSError, ET.ParseError) as exc:
        print(f"  [FAIL] no readable JUnit report ({exc})")
        return 1
    outcomes: dict[str, list[str]] = defaultdict(list)
    for case in root.iter("testcase"):
        base = case.get("name", "").split("[", 1)[0]
        result = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                result = tag
        outcomes[base].append(result)

    total = sum(len(v) for v in outcomes.values())
    problems = []
    if total == 0:
        problems.append("no integration tests were collected")
    for name, results in sorted(outcomes.items()):
        bad = [r for r in results if r != "passed"]
        if bad:
            problems.append(f"{name}: {', '.join(bad)}")
    for name in required:
        if name not in outcomes:
            problems.append(f"{name}: required but did not run")

    passed = sum(r == "passed" for v in outcomes.values() for r in v)
    print(f"  collected={total} passed={passed} required_functions={len(required)}")
    for problem in problems:
        print(f"  [FAIL] {problem}")
    if not problems:
        print("  [PASS] every required integration test ran and passed; none skipped")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
