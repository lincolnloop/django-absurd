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


def validate_args_is_list(value: t.Any) -> None:
    # The worker calls task(*args); a non-list (dict/scalar) would raise TypeError on
    # every fire, so reject the wrong shape here rather than at runtime.
    if not isinstance(value, list):
        msg = "args must be a JSON array (list)."
        raise ValidationError(msg)


def validate_kwargs_is_dict(value: t.Any) -> None:
    # The worker calls task(**kwargs); a non-dict (list/scalar) would raise TypeError on
    # every fire.
    if not isinstance(value, dict):
        msg = "kwargs must be a JSON object (dict)."
        raise ValidationError(msg)


def validate_headers_is_object(value: t.Any) -> None:
    # headers is an optional JsonObject; null is allowed (no headers), any other
    # non-dict shape is not.
    if value is not None and not isinstance(value, dict):
        msg = "headers must be a JSON object (dict)."
        raise ValidationError(msg)
