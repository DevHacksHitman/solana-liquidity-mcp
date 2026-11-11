"""End-to-end smoke client: drive the server exactly like an MCP host would.

Spawns ``mcp_server.py`` as a subprocess, performs the real handshake over
stdio, lists tools, calls two of them, reads the schema resource, and prints a
PASS/FAIL transcript. Doubles as the README's demo command and as evidence the
wire format is correct.

    python3 scripts/mcp_smoke_client.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"


class Host:
    """Minimal MCP stdio host."""

    def __init__(self, arguments=None):
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), *(arguments or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
        )
        self.counter = 0
        self.failures = []

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.counter += 1
            message["id"] = self.counter
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        if notify:
            return None
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed the stream after {method}")
        return json.loads(line)

    def check(self, label, condition, detail=""):
        state = "PASS" if condition else "FAIL"
        print(f"  [{state}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def main() -> int:
    host = Host()
    print("MCP stdio smoke test")

    init = host.send(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke-client", "version": "0.1.0"},
        },
    )
    result = init.get("result", {})
    host.check("initialize negotiates a protocol version", bool(result.get("protocolVersion")),
               result.get("protocolVersion", ""))
    host.check("server identifies itself", result.get("serverInfo", {}).get("name") == "solana-liquidity-mcp")
    host.send("notifications/initialized", notify=True)

    tools = host.send("tools/list").get("result", {}).get("tools", [])
    host.check("tools/list returns 5 tools", len(tools) == 5, f"got {len(tools)}")

    snapshot = host.send("tools/call", {"name": "liquidity_snapshot", "arguments": {"window_minutes": 60}})
    body = snapshot.get("result", {}).get("content", [{}])[0].get("text", "")
    host.check("liquidity_snapshot returns text", "events" in body, body.splitlines()[0] if body else "")
    host.check("liquidity_snapshot is not an error", snapshot.get("result", {}).get("isError") is False)

    mint_call = host.send("tools/call", {"name": "token_activity", "arguments": {"mint": "MINT-DEMO-A"}})
    mint_body = mint_call.get("result", {}).get("content", [{}])[0].get("text", "")
    host.check("token_activity answers for a known mint", "MINT-DEMO-A" in mint_body)

    status = host.send("tools/call", {"name": "pipeline_status", "arguments": {}}).get("result", {})
    structured = status.get("structuredContent", {})
    host.check("pipeline_status reports dedupe integrity",
               structured.get("dedupe", {}).get("duplicates") == 0)

    resources = host.send("resources/list").get("result", {}).get("resources", [])
    host.check("resources/list advertises the schema", len(resources) == 1)

    schema = host.send("resources/read", {"uri": "market://schema"}).get("result", {})
    host.check("resources/read returns the schema", "deduplicated" in
               schema.get("contents", [{}])[0].get("text", ""))

    unknown = host.send("tools/call", {"name": "nope", "arguments": {}})
    host.check("unknown tool is a JSON-RPC error", unknown.get("error", {}).get("code") == -32602)

    host.close()
    print(f"\n{'ALL CHECKS PASSED' if not host.failures else 'FAILURES: ' + ', '.join(host.failures)}")
    return 1 if host.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
