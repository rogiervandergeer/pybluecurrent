"""Test helper: assert an API response still matches its response model, and report drift.

Two kinds of divergence get different treatment, mirroring the client's own philosophy:

- A **declared** field that is missing or the wrong type is drift that breaks callers, so it
  **fails** the assertion (e.g. ``email: str`` coming back ``None``, or ``actual_p1`` changing
  from ``int`` to ``float``).
- An **undeclared** key is additive: the client tolerates it at runtime, so the check tolerates
  it too — but every such key is collected (deeply, deduped) and surfaced as a warning, giving a
  running overview of what the backend returns that we have not modelled yet.

The walk aggregates *all* problems in a single pass (typeguard on its own fails at the first bad
field), so one live run yields the complete picture rather than one mismatch at a time — which
matters because the credentialed suite is rate-limited to a single login per run.
"""

import warnings
from typing import Any, get_args, get_origin, get_type_hints, is_typeddict

from typeguard import TypeCheckError, check_type


def _walk(value: Any, model: Any, path: str, errors: list[str], undeclared: list[str]) -> None:
    if is_typeddict(model):
        if not isinstance(value, dict):
            errors.append(f"{path or '<root>'}: expected a dict, got {type(value).__name__}")
            return
        hints = get_type_hints(model)
        required = getattr(model, "__required_keys__", frozenset())
        for key in value:
            if key not in hints:
                undeclared.append(f"{path}{key}")
        for key, annotation in hints.items():
            if key in value:
                _walk(value[key], annotation, f"{path}{key}.", errors, undeclared)
            elif key in required:
                errors.append(f"{path}{key}: missing required key")
        return
    if get_origin(model) is list:
        if not isinstance(value, list):
            errors.append(f"{path or '<root>'}: expected a list, got {type(value).__name__}")
            return
        (element_model,) = get_args(model) or (Any,)
        for item in value:
            _walk(item, element_model, path, errors, undeclared)
        return
    # Leaf (scalar, or a union such as ``datetime | None``): let typeguard judge it.
    try:
        check_type(value, model)
    except TypeCheckError as error:
        errors.append(f"{path.rstrip('.') or '<root>'}: {error}")


def assert_model(value: Any, model: Any) -> None:
    """Assert a response matches its model; fail on declared drift, warn on undeclared keys.

    ``model`` may be a TypedDict or ``list[SomeTypedDict]``.
    """
    errors: list[str] = []
    undeclared: list[str] = []
    _walk(value, model, "", errors, undeclared)
    name = getattr(model, "__name__", str(model))
    if undeclared:
        warnings.warn(f"{name}: undeclared API keys (not yet modelled): {sorted(set(undeclared))}", stacklevel=2)
    if errors:
        raise AssertionError(f"{name} does not match the response:\n  " + "\n  ".join(sorted(set(errors))))
