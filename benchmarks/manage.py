import os
import sys
from pathlib import Path


def main() -> None:
    # `python benchmarks/manage.py` puts benchmarks/ on sys.path[0], not the repo
    # root, so `benchmarks.settings` would not import without this.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")

    from django.core.management import execute_from_command_line  # noqa: PLC0415

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
