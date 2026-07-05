# Contract Tests (Pact Provider Verification)

`Music CLI` is the **provider** in the Pact relationship. Consumer
integrations publish pact files; this suite verifies the running API against
them.

## Running

```bash
uv run pytest tests/contract
```

## Pact Sources

The pact file lives in `tests/contract/pacts/`. A Pact Broker can be wired in
later by setting the appropriate environment variables documented in
`pact-python`.

## Implementation

Two tests run in collection order:

1. `test_consumer_pact.py` drives the real `MusicClient` against a Pact **mock
   server** and writes `pacts/music-cli-client-music-cli-server.json`.
2. `test_provider_verify.py` starts the real FastAPI provider as a subprocess and
   verifies it against that pact file with `pact.Verifier`.

## Conventions

- Each test must be marked with `@pytest.mark.contract`.
- Provider states are defined per pact and wired into a state-handler endpoint
  that exists only during verification runs.
