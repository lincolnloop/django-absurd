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

Running more than one example at once? Override the published port:
`APP_PORT=8010 docker compose up`.

Tear down: `docker compose down -v`

## Test

```
uv run pytest
```
