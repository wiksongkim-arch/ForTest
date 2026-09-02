"""EIM DSL 样例与回归模拟器。"""

from __future__ import annotations

from typing import Any

from backend.eim.builder.compiler import compile_dsl
from backend.eim.models import CanonicalEvent, EIMDSL


def simulate(
    dsl: EIMDSL,
    event: CanonicalEvent | dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
    target_fields: set[str] | None = None,
) -> dict[str, Any]:
    """返回结构化证据，调用方可直接展示失败字段。"""

    canonical = event if isinstance(event, CanonicalEvent) else CanonicalEvent.model_validate(event)
    output = compile_dsl(dsl, target_fields=target_fields).execute(canonical)
    differences: dict[str, dict[str, Any]] = {}
    if expected is not None:
        actual = output or {}
        for key in sorted(set(actual) | set(expected)):
            if actual.get(key) != expected.get(key):
                differences[key] = {"expected": expected.get(key), "actual": actual.get(key)}
    return {
        "matched": output is not None,
        "output": output,
        "passed": expected is None or not differences,
        "differences": differences,
    }


def run_samples(
    dsl: EIMDSL,
    samples: list[dict[str, Any]],
    *,
    target_fields: set[str] | None = None,
) -> dict[str, Any]:
    results = [
        simulate(
            dsl,
            sample["input"],
            expected=sample.get("expected"),
            target_fields=target_fields,
        )
        for sample in samples
    ]
    return {
        "passed": all(item["passed"] for item in results),
        "total": len(results),
        "failed": sum(not item["passed"] for item in results),
        "results": results,
    }
