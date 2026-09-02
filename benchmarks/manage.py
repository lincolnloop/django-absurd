import os
import sys
from pathlib import Path


def main() -> None:
    # benchmarks/ is already on sys.path for `settings` and the workloads; the repo
    # root goes on for a task path outside the harness, which the suite measures.
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

    from django.core.management import execute_from_command_line  # noqa: PLC0415

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
