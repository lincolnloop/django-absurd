import typing as t

import pytest


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("os.getpid", "'os.getpid' is not a Django task."),
        (
            "tests.raises_on_import.anything",
            "task 'tests.raises_on_import.anything' could not be imported:",
        ),
        (
            "tests.tasks.not_a_task",
            "task 'tests.tasks.not_a_task' could not be imported:",
        ),
    ],
)
def test_bad_task_rejected(
    validate: t.Callable[..., str | None],
    path: str,
    message: str,
) -> None:
    # both subjects (model full_clean + core system check) via the fixture
    result = validate(task=path)
    assert result
    assert message in result
