"""End-to-end stdio tests plus the zero-dependency guarantee.

These spawn the real server as a subprocess, so they verify the wire format
(newline-delimited JSON on stdout, diagnostics on stderr) rather than only the
in-process dispatch.
"""

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"
LOCAL_MODULES = {"market_data", "tool_registry", "mcp_server"}


class TestStdioEndToEnd(unittest.TestCase):
    def host(self, *arguments):
        return subprocess.Popen(
            [sys.executable, str(SERVER), *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
        )

    def test_handshake_and_tool_call(self):
        process = self.host()
        try:
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "pipeline_status", "arguments": {}}},
            ]
            stdout, stderr = process.communicate(
                "\n".join(json.dumps(message) for message in messages) + "\n", timeout=30
            )
        finally:
            if process.poll() is None:
                process.kill()

        self.assertEqual(process.returncode, 0)
        lines = [line for line in stdout.splitlines() if line.strip()]
        # every stdout line must be a complete JSON document: no stray prints
        payloads = [json.loads(line) for line in lines]
        self.assertEqual(len(payloads), 3)  # the notification produces no reply
        self.assertEqual(payloads[0]["result"]["serverInfo"]["name"], "solana-liquidity-mcp")
        self.assertEqual(len(payloads[1]["result"]["tools"]), 5)
        self.assertFalse(payloads[2]["result"]["isError"])
        self.assertIn("solana-liquidity-mcp", stderr)

    def test_print_tools_exits_without_serving(self):
        completed = subprocess.run(
            [sys.executable, str(SERVER), "--print-tools"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        tools = json.loads(completed.stdout)
        self.assertEqual(len(tools), 5)
        self.assertEqual(completed.stderr, "")

    def test_missing_database_fails_loudly_on_stderr_only(self):
        completed = subprocess.run(
            [sys.executable, str(SERVER), "--db", "/nonexistent/stream.db"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=30,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")  # nothing may pollute the wire
        self.assertIn("init_db.py", completed.stderr)

    def test_survives_a_malformed_line_mid_session(self):
        process = self.host()
        try:
            stdout, _ = process.communicate(
                "{{{ not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n",
                timeout=30,
            )
        finally:
            if process.poll() is None:
                process.kill()
        payloads = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(payloads[0]["error"]["code"], -32700)
        self.assertEqual(payloads[1]["result"], {})


class TestZeroDependencies(unittest.TestCase):
    def test_every_module_imports_only_the_standard_library(self):
        offenders = {}
        for path in sorted(ROOT.rglob("*.py")):
            parts = set(path.relative_to(ROOT).parts)
            if {"node_modules", ".venv"} & parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        names.add(node.module.split(".")[0])
            external = sorted(
                name for name in names
                if name not in sys.stdlib_module_names and name not in LOCAL_MODULES
            )
            if external:
                offenders[str(path.relative_to(ROOT))] = external
        self.assertEqual(offenders, {}, f"third-party imports found: {offenders}")


if __name__ == "__main__":
    unittest.main()
