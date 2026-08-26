"""Comparing what the gate predicted against what was actually billed.

This is a **bias detector for estimators**, not a scoring system for deployments. It
never blocks anything: an accuracy figure is feedback for improving the tool, and wiring
it into a gate decision would turn the tool's own error budget into somebody else's
failed deployment.
"""

from __future__ import annotations

from cost_gate.feedback.accuracy import (
    AccuracyReport,
    ServiceAccuracy,
    compare,
    summarise,
)
from cost_gate.feedback.providers import (
    CostExplorerObservationProvider,
    FixtureObservationProvider,
    ObservationError,
    ObservationProvider,
    observations_for,
    settled_window,
)
from cost_gate.feedback.records import (
    Comparability,
    Observation,
    PredictionRecord,
    PredictionStore,
    ServiceObservation,
    ServicePrediction,
)

__all__ = [
    "AccuracyReport",
    "Comparability",
    "CostExplorerObservationProvider",
    "FixtureObservationProvider",
    "Observation",
    "ObservationError",
    "ObservationProvider",
    "PredictionRecord",
    "PredictionStore",
    "ServiceAccuracy",
    "ServiceObservation",
    "ServicePrediction",
    "compare",
    "observations_for",
    "settled_window",
    "summarise",
]
