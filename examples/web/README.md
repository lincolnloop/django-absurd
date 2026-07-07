# django-absurd — web example

Demonstrates enqueue + result with django-absurd and nanodjango.

- Submit `add(a, b)` via a form; the worker picks it up and stores the result.
- Watch the task status page auto-refresh until the result appears.
- Browse queue tables in the auto-registered admin.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `http://localhost:8000/` — enqueue `add(a, b)`
- `http://localhost:8000/admin/` — read-only queue tables (login: **admin** / **admin**)

Tear down: `docker compose down -v`
