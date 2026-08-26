"""Finding and reading scenarios from disk.

Scenarios are authored under ``examples/scenarios/`` so that they are reviewable in the
repository, and force-included into the wheel at ``cost_gate/_data/scenarios`` so that
``cost-gate demo`` still works from an installed package. This mirrors how the pricing
catalog is handled, deliberately: two different answers to the same question would be
one more thing for a reader to hold in their head.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from cost_gate.config.errors import ConfigError
from cost_gate.config.loader import load_model
from cost_gate.demo.models import Scenario

__all__ = [
    "BASELINE_DIRECTORY",
    "BASELINE_FILENAME",
    "MANIFEST_FILENAME",
    "PROPOSED_DIRECTORY",
    "PROPOSED_FILENAME",
    "ScenarioError",
    "default_scenario_path",
    "load_scenario",
    "load_scenarios",
    "snapshot_path",
]

MANIFEST_FILENAME: Final = "scenario.yaml"
BASELINE_FILENAME: Final = "baseline.yaml"
PROPOSED_FILENAME: Final = "proposed.yaml"
BASELINE_DIRECTORY: Final = "baseline"
PROPOSED_DIRECTORY: Final = "proposed"

MAX_SCENARIOS: Final = 200
"""A directory with more than this is a mistake, not a demo suite."""


class ScenarioError(Exception):
    """A scenario could not be loaded.

    Distinct from a scenario that ran and failed its expectation: this means the
    demonstration itself is broken, which is the author's bug rather than the tool's.
    """


def default_scenario_path() -> Path:
    """Where scenarios live.

    An installed wheel carries them at ``cost_gate/_data/scenarios``; a source checkout
    has them at ``examples/scenarios/``, which is where they are authored.
    """
    packaged = Path(__file__).resolve().parent.parent / "_data" / "scenarios"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "examples" / "scenarios"


def load_scenario(directory: Path) -> Scenario:
    """Load one scenario from its directory.

    Raises:
        ScenarioError: if the manifest or either template is missing or invalid.
    """
    manifest = directory / MANIFEST_FILENAME
    if not manifest.is_file():
        raise ScenarioError(f"{directory} has no {MANIFEST_FILENAME}")

    try:
        scenario = load_model(Scenario, manifest)
    except ConfigError as exc:
        raise ScenarioError(exc.render()) from exc

    if scenario.identifier != directory.name:
        # Otherwise `--scenario x` and the directory holding x disagree, and the error
        # message points at the wrong place.
        raise ScenarioError(
            f"scenario in {directory} declares id {scenario.identifier!r}, "
            f"which does not match its directory name"
        )
    for side in ("baseline", "proposed"):
        if snapshot_path(directory, side) is None:
            raise ScenarioError(
                f"scenario {scenario.identifier} has neither {side}.yaml nor a {side}/ directory"
            )
    return scenario


def snapshot_path(directory: Path, side: str) -> Path | None:
    """Locate one side of a scenario, as a file or a directory.

    A single template covers most scenarios, but a CDK app synthesises to one template
    per stack, and matching is scoped per stack — so a multi-stack change cannot be
    expressed as a single file without losing exactly the structure that makes it worth
    demonstrating. Both shapes load through the same ``load_graph``.

    Returns:
        The file or directory, or ``None`` if neither exists.
    """
    single = directory / f"{side}.yaml"
    if single.is_file():
        return single
    multiple = directory / side
    if multiple.is_dir():
        return multiple
    return None


def load_scenarios(root: Path | None = None) -> list[tuple[Scenario, Path]]:
    """Load every scenario under ``root``, sorted by identifier.

    Sorted because the demo output, like every other output of this tool, must not
    depend on the order a filesystem happens to return directories in.

    Raises:
        ScenarioError: if the root is missing or any scenario is invalid. One broken
            scenario fails the load rather than being skipped: a demo suite that
            silently runs fewer cases than it contains is worse than one that stops.
    """
    directory = (root or default_scenario_path()).resolve()
    if not directory.is_dir():
        raise ScenarioError(f"no scenario directory at {directory}")

    candidates = sorted(path for path in directory.iterdir() if path.is_dir())
    if len(candidates) > MAX_SCENARIOS:
        raise ScenarioError(f"{directory} holds {len(candidates)} scenarios; too many to run")

    loaded = [(load_scenario(path), path) for path in candidates]
    if not loaded:
        raise ScenarioError(f"{directory} contains no scenarios")

    identifiers = [scenario.identifier for scenario, _ in loaded]
    duplicates = {name for name in identifiers if identifiers.count(name) > 1}
    if duplicates:
        raise ScenarioError(f"duplicate scenario ids: {', '.join(sorted(duplicates))}")
    return sorted(loaded, key=lambda pair: pair[0].identifier)
