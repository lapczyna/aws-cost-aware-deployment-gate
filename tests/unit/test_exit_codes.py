"""The exit-code mapping is a public contract.

Branch protection and downstream deployment jobs depend on these numbers, so they are
pinned by a test rather than left to drift with the enum definition.
"""

from __future__ import annotations

import pytest

from cost_gate.exit_codes import ExitCode

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("code", "value"),
    [
        (ExitCode.PASS, 0),
        (ExitCode.REQUIRE_APPROVAL, 10),
        (ExitCode.BLOCK, 20),
        (ExitCode.ERROR, 30),
        (ExitCode.USAGE, 64),
    ],
)
def test_documented_exit_code_values(code: ExitCode, value: int):
    assert int(code) == value


def test_only_pass_is_successful():
    successful = [code for code in ExitCode if code == 0]
    assert successful == [ExitCode.PASS]


def test_error_does_not_succeed():
    # A gate that opens when it is confused is not a gate.
    assert ExitCode.ERROR != 0
