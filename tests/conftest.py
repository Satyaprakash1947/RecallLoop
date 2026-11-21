import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((PROJECT_ROOT / "data" / "golden_test_cases.json").read_text(encoding="utf-8"))


def pytest_configure(config):
    config._recallloop_case_results = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    case_id = getattr(item, "callspec", None)
    if report.when == "call":
        case = case_id.params.get("case") if case_id and "case" in case_id.params else None
        key = case["id"] if case else item.name
        item.config._recallloop_case_results[key] = {
            "description": case["description"] if case else item.name,
            "passed": report.passed,
            "detail": "passed" if report.passed else str(report.longrepr).splitlines()[-1],
            "golden": case is not None,
        }


def pytest_sessionfinish(session, exitstatus):
    results = getattr(session.config, "_recallloop_case_results", {})
    if not results:
        return
    report_path = PROJECT_ROOT / "reports" / "golden_test_report.md"
    report_path.parent.mkdir(exist_ok=True)
    passed = sum(r["passed"] for r in results.values())
    golden = [r for r in results.values() if r["golden"]]
    rows = ["# RecallLoop Test Report", "", f"Full suite: {passed}/{len(results)} passed",
            f"Golden cases: {sum(r['passed'] for r in golden)}/{len(golden)} passed", "",
            "| Case or test | Scenario | Result | Detail |", "|---|---|---|---|"]
    for case_id in sorted(results):
        row = results[case_id]
        detail = row["detail"].replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {case_id} | {row['description']} | {'PASS' if row['passed'] else 'FAIL'} | {detail} |")
    report_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
