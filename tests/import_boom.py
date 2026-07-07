"""Test fixture module that raises a non-ImportError at import time.

Used to prove the pg_cron effective-queue check reports E007 rather than
crashing `manage.py check` when a scheduled task's module errors on import
(e.g. a module-level env-var read raising KeyError/ValueError).
"""

msg = "boom at import time"
raise ValueError(msg)
