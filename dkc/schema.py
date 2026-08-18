"""Schema loading and validation.

Records are validated against the JSON Schema files in `schemas/` rather than
against the dataclasses alone. The dataclasses enforce the invariants that need
code; the schemas fix the wire format that other tools, and future versions of
this project, have to agree on.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any

__all__ = ["SCHEMA_DIR", "load", "validate"]

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "schemas"
def load(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"no schema named {name!r} in {SCHEMA_DIR}")
    with path.open(encoding="utf-8") as handle:
        result: dict[str, Any] = json.load(handle)
    return result


def validate(name: str, document: Any) -> None:
    """Validate a document, raising on the first problem.

    jsonschema is a build and test dependency, not a runtime one for the
    publication path; importing it here keeps that boundary visible.
    """
    import jsonschema  # type: ignore[import-untyped]

    checker = jsonschema.FormatChecker()

    @checker.checks("date-time")  # type: ignore[misc]
    def is_date_time(value: object) -> bool:
        if not isinstance(value, str):
            return True
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    validator = jsonschema.Draft202012Validator(load(name), format_checker=checker)
    validator.validate(document)
