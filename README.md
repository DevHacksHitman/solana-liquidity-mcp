# Solana Liquidity MCP Server

A **zero-dependency** Model Context Protocol server that exposes a Solana
market-data store to AI agents as five read-only tools.

    python3 mcp_server.py          # demo fixture — keyless, offline, instant

| | |
|---|---|
| Tools | **5** (+1 schema resource) |
| Dependencies | **0** — standard library only |
| Python | 3.10 – 3.13 (CI matrix) |
| Tests | **59**, zero-network, no packages installed in CI |
| Code | **971 lines** across 3 modules |
| Mean tool-call latency | **0.063 ms** (in-process, 84-event store) |
| Network calls in tests | **0** |

## Why this exists

[`solana-liquidity-stream-tracker`](https://github.com/DevHacksHitman/solana-liquidity-stream-tracker)
records on-chain trade events into a SQLite store. Recording data is only half
the job: an agent that can *ask questions* of that store is more useful than
one that can only read raw rows.

This server is the typed, read-only interface. It speaks MCP over stdio, so any
MCP-capable client (Claude Desktop, an IDE agent, a custom host) can call it
directly — no SDK, no HTTP server, no daemon.

## Quickstart

    # 1. Demo mode: synthetic 84-event fixture, no network, no database
    python3 mcp_server.py

    # 2. Point it at the tracker's live database (opened read-only)
    python3 mcp_server.py --db /path/to/tracker/data/stream.db

    # 3. Inspect the tool schemas without connecting a client
    python3 mcp_server.py --print-tools

    # 4. Prove the whole wire path end to end (spawns the server for real)
    python3 scripts/mcp_smoke_client.py

## Wire it into an MCP client

Claude Desktop / Cursor / any stdio MCP host — add to the client's server
config and restart it:

```json
{
  "mcpServers": {
    "solana-liquidity": {
      "command": "python3",
      "args": ["/absolute/path/to/solana-liquidity-mcp/mcp_server.py"]
    }
  }
}
```

To serve the real store instead of the demo fixture, append
`"--db", "/absolute/path/to/tracker/data/stream.db"` to `args`.

## Tools

| Tool | Answers |
|---|---|
| `liquidity_snapshot` | How much activity, across how many mints, over what window — plus the largest quiet gap (a pipeline-health signal) |
| `recent_events` | The newest events, filtered by mint and/or side |
| `token_activity` | Everything about one mint: counts by side, first/last seen, mean interval; distinguishes *never observed* from *quiet lately* |
| `list_mints` | Mints ranked by activity, with last-seen timestamps |
| `pipeline_status` | What is connected, how many rows, the time span, and a signature-uniqueness audit |

Example — `tools/call` with `{"name": "liquidity_snapshot", "arguments": {"window_minutes": 60}}`:

    **84 events** across **4 mints** in the last 60 min (~14.12/min)

    Window: 2026-10-28T18:00:05Z → 2026-10-28T18:06:02Z
    Sides: {'buy': 54, 'sell': 30}
    Largest inter-arrival gap: 95s

    | mint | events |
    | --- | --- |
    | MINT-DEMO-B | 25 |
    | MINT-DEMO-C | 20 |
    | MINT-DEMO-A | 17 |
    | MINT-DEMO-D | 13 |

Every tool returns human-readable markdown **and** `structuredContent` for
clients that consume typed output.

## Design decisions

**No SDK.** The MCP surface this server needs is small: `initialize`, `ping`,
`tools/list`, `tools/call`, `resources/list`, `resources/read`. Implementing it
directly on the standard library removes a dependency chain and makes the
protocol behaviour auditable in ~300 lines. A test asserts that **every Python
file in this repository imports only the standard library**.

**stdout is the wire.** Messages are newline-delimited JSON-RPC 2.0 on
stdout; all diagnostics go to stderr. A stray `print` would corrupt the
protocol, so a test drives the real subprocess and asserts that every stdout
line parses as exactly one JSON document.

**Read-only by construction.** The SQLite backend opens with a `mode=ro` URI —
the connected agent physically cannot write to your store. It deliberately
does *not* use `immutable=1`: the tracker may be writing live, and an immutable
handle on a moving database is how read corruption errors happen.

**Windows are relative to the newest stored event**, not wall-clock time. That
keeps bundled demo data meaningful offline, and in live use the newest event is
the horizon an operator cares about. The rule is applied identically in every
tool — a bug where one tool disagreed was caught by the test suite.

**One bad message never kills the session.** Malformed lines produce a JSON-RPC
error and the loop continues; unknown methods, bad params, unknown tools and
handler exceptions all return spec-shaped errors. Covered by tests.

**Synthetic identifiers in fixtures.** The bundled fixture uses
`sig-demo-0001` / `MINT-DEMO-A` style names so the repository contains no
key-shaped or address-shaped literals — the same hygiene rule the tracker's
test fixtures follow.

## Compatibility

| | |
|---|---|
| Protocol versions | `2025-06-18`, `2025-03-26`, `2024-11-05` (newest is offered when a client requests an unknown version, per the MCP negotiation rule) |
| Transport | stdio (newline-delimited JSON-RPC 2.0) |
| Capabilities | `tools`, `resources` |
| Verified by | `scripts/mcp_smoke_client.py` — real subprocess, real handshake |

## Honest limitations

- **No live RPC mode yet.** The server reads a fixture or a SQLite store; it
  does not itself subscribe to a WebSocket. The tracker is the recorder; this
  is the query surface.
- **Batch requests are rejected** (`-32600`) rather than processed: MCP hosts
  do not use them, and pretending otherwise would be untested surface.
- **The demo fixture is synthetic** — 84 generated events, not real market
  history. It exists so the server is demonstrable and testable without keys.
- **`token_amount` / `sol_amount` / `trader` are frequently NULL** because the
  listener stores what the RPC actually returned rather than inferring values.
- **No authentication.** stdio means the transport is a local process; do not
  wrap this in a network listener without adding auth.
- Aggregations scan the loaded event list in memory. That is honest for tens of
  thousands of rows; it is not a streaming analytics engine.

## Tests

    python3 -m unittest discover -s tests -t . -v

59 tests, all zero-network: protocol dispatch and error codes, both stores
(including read-only enforcement against the tracker's own DDL), window
semantics, every tool's output, the subprocess stdio path, and the
standard-library-only guarantee.

## Repository hygiene

- Commits use the GitHub noreply identity; no personal addresses in history.
- `githooks/pre-push` is a blocking sensitivity gate. Enable it once per clone:

      git config core.hooksPath githooks

  Its needles are written self-avoidantly (character-class breaks) so the hook
  can never match its own source and block every push forever.

## Roadmap

- Live mode: subscribe directly (`logsSubscribe`) with the same tool interface.
- Pagination cursors on `recent_events` for large stores.
- A `prompts` capability with canned analysis prompts over the tools.
- SQLite-side aggregation for stores beyond memory scale.

## License

MIT — see `LICENSE`.
