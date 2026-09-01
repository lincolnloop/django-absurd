import os
import sys
from pathlib import Path


def main() -> None:
    # Running this file as a script already puts benchmarks/ on sys.path, which is
    # where `settings` and the workloads come from. The repo root goes on for a task
    # path that lives outside the harness — the test suite measures workloads of its
    # own.
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

    from django.core.management import execute_from_command_line  # noqa: PLC0415

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
