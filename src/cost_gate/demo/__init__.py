"""Deterministic demonstration scenarios.

Each scenario is a pair of CloudFormation snapshots plus a hand-written statement of
what the gate is expected to do with them. Running one requires no AWS account, no
credentials and no network access.
"""

from __future__ import annotations

from cost_gate.demo.loader import (
    ScenarioError,
    default_scenario_path,
    load_scenario,
    load_scenarios,
)
from cost_gate.demo.models import Expectation, Scenario, ScenarioOutcome
from cost_gate.demo.runner import check_expectation, run_scenario

__all__ = [
    "Expectation",
    "Scenario",
    "ScenarioError",
    "ScenarioOutcome",
    "check_expectation",
    "default_scenario_path",
    "load_scenario",
    "load_scenarios",
    "run_scenario",
]
