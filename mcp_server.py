"""A zero-dependency MCP server for Solana market data.

Speaks the Model Context Protocol over stdio: newline-delimited JSON-RPC 2.0
messages on stdin/stdout, logs on stderr. No SDK, no third-party packages —
the protocol surface this server needs is small enough to implement directly,
and each dependency removed is a supply-chain surface removed.

    python3 mcp_server.py                  # demo fixture (keyless, offline)
    python3 mcp_server.py --db data/stream.db   # the tracker's SQLite store
    python3 mcp_server.py --print-tools    # dump the tool schemas

Wire rules that matter:
* stdout carries **only** protocol messages; anything printed to stdout by
  accident corrupts the stream, so all diagnostics go to stderr.
* A malformed line produces a JSON-RPC error and the loop continues — one bad
  message never kills the session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

import market_data
import tool_registry

SERVER_NAME = "solana-liquidity-mcp"
SERVER_VERSION = "0.1.0"

# Newest first. The server replies with the newest it supports when a client
# asks for something it does not know (per the MCP version-negotiation rule).
SUPPORTED_PROTOCOL_VERSIONS: List[str] = ["2025-06-18", "2025-03-26", "2024-11-05"]
PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SCHEMA_RESOURCE_URI = "market://schema"

SCHEMA_MARKDOWN = """# Market-data store schema

One table, one row per observed on-chain trade event, deduplicated by
transaction signature (a `UNIQUE` constraint — the store cannot hold the same
signature twice).

| column | type | meaning |
|---|---|---|
| `signature` | TEXT UNIQUE | transaction signature |
| `slot` | INTEGER | Solana slot the event landed in |
| `program_id` | TEXT | program under observation |
| `side` | TEXT | `buy`, `sell`, or NULL when the logs are ambiguous |
| `mint` | TEXT | token mint, when known |
| `trader` | TEXT | trader address, when known |
| `token_amount` / `sol_amount` | REAL | amounts from fetched metadata, else NULL |
| `raw_logs` | TEXT | program log lines (truncated) |
| `metadata_json` | TEXT | pruned transaction metadata |
| `fetched_at` | TIMESTAMP | observation time, UTC |

All read paths are read-only. Windows are measured from the newest event.
Amounts and counterparties are frequently NULL: the listener stores what the
RPC actually returned rather than inferring values.
"""

INSTRUCTIONS = (
    "Read-only market-data tools for a Solana liquidity monitor. Start with "
    "pipeline_status to see what is connected, then liquidity_snapshot for the "
    "shape of recent activity. All windows are measured from the newest stored "
    "event, so 'last 60 minutes' means 'the 60 minutes before the newest event'."
)


class JsonRpcError(Exception):
    """An error that maps to a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Optional[Any] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _log(message: str) -> None:
    """Diagnostics go to stderr — stdout is the protocol wire."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


class Server:
    """Protocol dispatch. Pure request -> response; no I/O of its own."""

    def __init__(self, store: Any, *, log: bool = True) -> None:
        self.store = store
        self.log = log

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, error: JsonRpcError) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            payload["data"] = error.data
        return {"jsonrpc": "2.0", "id": request_id, "error": payload}

    # -- methods ------------------------------------------------------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            negotiated = requested
        else:
            negotiated = PREFERRED_PROTOCOL_VERSION
        if self.log and requested and negotiated != requested:
            _log(f"client asked for protocol {requested}; replying with {negotiated}")
        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a tool name")
        try:
            markdown, data = tool_registry.call_tool(
                self.store, name, params.get("arguments")
            )
        except tool_registry.UnknownTool:
            known = ", ".join(tool["name"] for tool in tool_registry.TOOLS)
            raise JsonRpcError(
                INVALID_PARAMS, f"unknown tool: {name}", {"known_tools": known}
            ) from None
        except tool_registry.InvalidArguments as exc:
            raise JsonRpcError(
                INVALID_PARAMS, f"invalid arguments for {name}: {exc}"
            ) from None
        return {
            "content": [{"type": "text", "text": markdown}],
            "structuredContent": data,
            "isError": False,
        }

    @staticmethod
    def _resources_read(params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if uri != SCHEMA_RESOURCE_URI:
            raise JsonRpcError(INVALID_PARAMS, f"unknown resource: {uri!r}")
        return {
            "contents": [
                {
                    "uri": SCHEMA_RESOURCE_URI,
                    "mimeType": "text/markdown",
                    "text": SCHEMA_MARKDOWN,
                }
            ]
        }

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Handle one decoded message; return a response, or None for notifications."""
        if not isinstance(message, dict):
            return self._error(None, JsonRpcError(INVALID_REQUEST, "request must be an object"))
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, JsonRpcError(INVALID_PARAMS, "params must be an object"))
        is_notification = "id" not in message

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(request_id, JsonRpcError(INVALID_REQUEST, "invalid JSON-RPC request"))

        try:
            if method == "initialize":
                return self._result(request_id, self._initialize(params))
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": tool_registry.tool_specs()})
            if method == "tools/call":
                return self._result(request_id, self._tools_call(params))
            if method == "resources/list":
                return self._result(
                    request_id,
                    {
                        "resources": [
                            {
                                "uri": SCHEMA_RESOURCE_URI,
                                "name": "Market-data schema",
                                "description": "Tables, columns and their meaning.",
                                "mimeType": "text/markdown",
                            }
                        ]
                    },
                )
            if method == "resources/read":
                return self._result(
                    request_id, self._resources_read(params)
                )
            if method in ("notifications/initialized", "notifications/cancelled",
                          "notifications/roots/list_changed", "logging/setLevel"):
                if method == "logging/setLevel":
                    return self._result(request_id, {}) if not is_notification else None
                return None
            raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")
        except JsonRpcError as error:
            if is_notification:
                if self.log:
                    _log(f"notification {method} failed: {error.message}")
                return None
            return self._error(request_id, error)
        except Exception as exc:  # never let an unexpected error kill the session
            if self.log:
                _log(f"internal error handling {method}: {exc!r}")
            if is_notification:
                return None
            return self._error(
                request_id, JsonRpcError(INTERNAL_ERROR, f"internal error: {exc!r}")
            )


def serve(server: Server, stdin: TextIO, stdout: TextIO) -> int:
    """Read newline-delimited JSON-RPC from ``stdin``, write responses to ``stdout``."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            response = server._error(None, JsonRpcError(PARSE_ERROR, f"invalid JSON: {exc}"))
        else:
            if isinstance(message, list):
                response = server._error(
                    None,
                    JsonRpcError(INVALID_REQUEST, "batch requests are not supported"),
                )
            else:
                response = server.handle(message)
        if response is None:
            continue
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0


def build_store(args: argparse.Namespace) -> Any:
    if args.db:
        return market_data.SqliteStore(args.db)
    return market_data.FixtureStore(args.fixture or market_data.FIXTURE_DEFAULT)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="Zero-dependency MCP server over a Solana market-data store",
    )
    parser.add_argument("--db", metavar="PATH", help="tracker SQLite store (read-only)")
    parser.add_argument("--fixture", metavar="PATH", help="JSONL event fixture")
    parser.add_argument(
        "--print-tools", action="store_true", help="dump tool schemas as JSON and exit"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress stderr diagnostics"
    )
    parser.add_argument("--version", action="version", version=f"{SERVER_NAME} {SERVER_VERSION}")
    args = parser.parse_args(argv)

    try:
        store = build_store(args)
    except (FileNotFoundError, ValueError) as exc:
        _log(str(exc))
        return 2

    if args.print_tools:
        print(json.dumps(tool_registry.tool_specs(), indent=2))
        return 0

    _log(f"serving {store.describe()} ({len(store.events)} events) over stdio")
    server = Server(store, log=not args.quiet)
    try:
        return serve(server, sys.stdin, sys.stdout)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
