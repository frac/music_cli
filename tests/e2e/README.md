# End-to-End Tests

These tests exercise the full stack against real backing services. They are
**not** run by the default `uv run pytest` invocation.

## Running

```bash
docker compose up -d           # start backing services
uv run pytest tests/e2e        # run e2e suite
docker compose down            # tear down
```

## Conventions

- Each test must be marked with `@pytest.mark.e2e`.
- Tests assume services are reachable on their default ports.
- Do not add e2e tests to `tests/unit/` or `tests/integration/` — they will be
  picked up by the default test run and require Docker.
