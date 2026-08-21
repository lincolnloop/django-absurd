# django-absurd — web example

Demonstrates enqueue + result with django-absurd and nanodjango.

- Submit `add(a, b)` via a form; the worker picks it up and stores the result.
- Watch the task status page auto-refresh until the result appears.
- Run an order-fulfillment workflow mirroring
  [Absurd's headline example](https://github.com/earendil-works/absurd#readme) (charge →
  reserve inventory → wait → notify) to see Steps (checkpoints) and Events (it suspends
  on `await_event` until a button emits the matching event).
- Browse queue tables in the auto-registered admin.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `http://localhost:8000/` — enqueue `add(a, b)`
- `http://localhost:8000/admin/` — read-only queue tables (login: **admin** / **admin**)

Running more than one example at once? Override the published port:
`APP_PORT=8010 docker compose up`.

Tear down: `docker compose down -v`

## Test

Runs in the demo's own Docker stack, the way CI runs it:

```
docker compose up -d --build --wait db
docker compose run --rm --build app sh -c "cd /app && pytest"
```
