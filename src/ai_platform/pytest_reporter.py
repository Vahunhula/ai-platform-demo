"""Tiny pytest plugin that emits deterministic machine-readable result identities."""

import json
import os
from pathlib import Path

_failures: set[str] = set()
_passed = 0
_collected = 0


def pytest_collection_finish(session) -> None:
    global _collected
    _collected = len(session.items)


def pytest_collectreport(report) -> None:
    if report.failed:
        _failures.add(report.nodeid or "<collection>")


def pytest_runtest_logreport(report) -> None:
    global _passed
    if report.when == "call" and report.passed:
        _passed += 1
    if report.failed:
        suffix = "" if report.when == "call" else f" [{report.when}]"
        _failures.add(f"{report.nodeid}{suffix}")


def pytest_sessionfinish(session, exitstatus) -> None:
    target = os.environ.get("AI_PLATFORM_PYTEST_REPORT")
    if target:
        Path(target).write_text(
            json.dumps(
                {
                    "exit_code": int(exitstatus),
                    "failures": sorted(_failures),
                    "passed": _passed,
                    "collected": _collected,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
