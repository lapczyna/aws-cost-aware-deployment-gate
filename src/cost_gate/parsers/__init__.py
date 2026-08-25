"""Infrastructure-as-Code templates to normalised resource graphs.

Three stages, each with one job:

1. :mod:`cost_gate.parsers.cfn_loader` — read the text safely. Bounded ``SafeLoader``
   with CloudFormation shorthand tag support; JSON goes through the same path because
   JSON is a subset of YAML.
2. :mod:`cost_gate.parsers.intrinsics` — decide what each expression means, and say so
   honestly when it cannot be decided before deployment.
3. :mod:`cost_gate.parsers.normalize` — flatten to JSON Pointer paths and attach
   identity, tags, attribution and source location.

The whole layer holds one rule: a value that cannot be established becomes an explicit
unknown, never a plausible default.
"""

from __future__ import annotations

from cost_gate.parsers.cfn_loader import (
    SHORTHAND_TAGS,
    CfnSafeLoader,
    load_template_file,
    load_template_text,
)
from cost_gate.parsers.errors import TemplateError, TemplateIssue
from cost_gate.parsers.intrinsics import (
    Known,
    Omitted,
    Reference,
    Resolution,
    ResolutionContext,
    Unknown,
    evaluate_condition,
    resolve,
)
from cost_gate.parsers.normalize import (
    CONTEXT_TAG_KEYS,
    load_graph,
    load_graph_from_text,
    normalize_template,
)

__all__ = [
    "CONTEXT_TAG_KEYS",
    "SHORTHAND_TAGS",
    "CfnSafeLoader",
    "Known",
    "Omitted",
    "Reference",
    "Resolution",
    "ResolutionContext",
    "TemplateError",
    "TemplateIssue",
    "Unknown",
    "evaluate_condition",
    "load_graph",
    "load_graph_from_text",
    "load_template_file",
    "load_template_text",
    "normalize_template",
    "resolve",
]
