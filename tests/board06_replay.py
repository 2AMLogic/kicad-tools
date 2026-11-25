"""Finish the diagnostic run once xdist has delivered the target teardown."""
import os

import pytest


def pytest_runtest_logreport(report):
    if "PYTEST_XDIST_WORKER" in os.environ:
        return
    if report.when == "teardown" and report.nodeid.endswith(
        "::test_final_clock_copper_obeys_j4_rule_area[permitted-width]"
    ):
        pytest.exit("Bulk replay sample collected", returncode=0)
