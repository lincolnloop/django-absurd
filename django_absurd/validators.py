"""Shared schedule validators used by both the system checks and model-first
validation. Pure and framework-light: each raises
`django.core.exceptions.ValidationError`; the checks wrap failures into
`absurd.E007`. Lives in core (not the pg_cron app) because the settings-side
checks — which run for every scheduler — depend on it.
"""

import json
import typing as t

from django.core.exceptions import ValidationError
from django.tasks import Task
from django.utils.module_loading import import_string


def validate_task_path(value: t.Any) -> None:
    try:
        task_obj = import_string(value)
    except Exception as exc:
        msg = f"task {value!r} could not be imported: {exc!r}"
        raise ValidationError(msg, code="task_import") from exc
    if not isinstance(task_obj, Task):
        msg = f"{value!r} is not a Django task."
        raise ValidationError(msg, code="not_a_task")


def make_json_serializable_validator(field: str) -> t.Callable[[t.Any], None]:
    def validate(value: t.Any) -> None:
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            msg = f"{field} is not JSON-serializable."
            raise ValidationError(msg) from exc

    return validate


validate_args_serializable = make_json_serializable_validator("args")
validate_kwargs_serializable = make_json_serializable_validator("kwargs")
