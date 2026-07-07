# django-absurd — beat example

Demonstrates the BEAT scheduler with django-absurd and nanodjango.

- The worker (started with `--beat`) fires `tick` every minute.
- Each run logs `tock ⏰` — watch it appear in the worker logs.
- Browse queue tables and task runs in the auto-registered admin.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `docker compose logs worker` — watch for `tock ⏰` each minute
- `http://localhost:8000/admin/` — Tasks / Runs growing (login: **admin** / **admin**)

Tear down: `docker compose down -v`
