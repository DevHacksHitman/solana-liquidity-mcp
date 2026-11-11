"""Protocol tests: handshake, dispatch, error codes, resilience."""

import io
import json
import unittest

import market_data
import mcp_server


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mcp_server.Server(market_data.FixtureStore(), log=False)
        cls.other_server = market_data.FixtureStore()

    def test_initialize_echoes_supported_version(self):
        result = self.server.handle(request("initialize", {"protocolVersion": "2024-11-05"}))["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "solana-liquidity-mcp")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("resources", result["capabilities"])
        self.assertTrue(result["instructions"])

    def test_initialize_falls_back_on_unknown_version(self):
        result = self.server.handle(request("initialize", {"protocolVersion": "1999-01-01"}))["result"]
        self.assertEqual(result["protocolVersion"], mcp_server.PREFERRED_PROTOCOL_VERSION)

    def test_ping(self):
        response = self.server.handle(request("ping"))
        self.assertEqual(response["result"], {})

    def test_ids_are_echoed_including_zero_and_strings(self):
        for request_id in (0, "abc-123"):
            response = self.server.handle(request("ping", request_id=request_id))
            self.assertEqual(response["id"], request_id)

    def test_notifications_get_no_response(self):
        self.assertIsNone(
            self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "unknown/notification"}))

    def test_unknown_method(self):
        response = self.server.handle(request("market/predict"))
        self.assertEqual(response["error"]["code"], mcp_server.METHOD_NOT_FOUND)

    def test_invalid_request_shapes(self):
        for message in ("just a string", {"jsonrpc": "1.0", "id": 1, "method": "ping"}):
            response = self.server.handle(message)
            self.assertEqual(response["error"]["code"], mcp_server.INVALID_REQUEST)

    def test_params_must_be_an_object(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]})
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_tools_list(self):
        tools = self.server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual(len(tools), 5)
        self.assertTrue(all("inputSchema" in tool for tool in tools))

    def test_tools_call_returns_text_and_structured_content(self):
        response = self.server.handle(
            request("tools/call", {"name": "liquidity_snapshot", "arguments": {"window_minutes": 30}})
        )
        result = response["result"]
        self.assertEqual(result["isError"], False)
        self.assertTrue(result["content"][0]["text"].strip())
        self.assertEqual(result["structuredContent"]["events"], 84)

    def test_tools_call_unknown_tool_is_invalid_params(self):
        response = self.server.handle(request("tools/call", {"name": "nope", "arguments": {}}))
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)
        self.assertIn("known_tools", response["error"]["data"])

    def test_tools_call_invalid_arguments(self):
        response = self.server.handle(
            request("tools/call", {"name": "recent_events", "arguments": {"limit": 9999}})
        )
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)

        response = self.server.handle(request("tools/call", {"arguments": {}}))
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_resources(self):
        resources = self.server.handle(request("resources/list"))["result"]["resources"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["uri"], mcp_server.SCHEMA_RESOURCE_URI)

        contents = self.server.handle(
            request("resources/read", {"uri": mcp_server.SCHEMA_RESOURCE_URI})
        )["result"]["contents"]
        self.assertIn("deduplicated", contents[0]["text"])

        response = self.server.handle(request("resources/read", {"uri": "market://nope"}))
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_logging_setlevel_is_accepted(self):
        response = self.server.handle(request("logging/setLevel", {"level": "debug"}))
        self.assertEqual(response["result"], {})

    def test_exceptions_never_escape(self):
        broken = mcp_server.Server(object(), log=False)  # no .events attribute
        response = broken.handle(request("tools/call", {"name": "pipeline_status", "arguments": {}}))
        self.assertEqual(response["error"]["code"], mcp_server.INTERNAL_ERROR)


class TestServe(unittest.TestCase):
    def run_serve(self, lines):
        server = mcp_server.Server(market_data.FixtureStore(), log=False)
        out = io.StringIO()
        mcp_server.serve(server, io.StringIO("".join(line + "\n" for line in lines)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_garbage_line_is_reported_and_loop_survives(self):
        responses = self.run_serve(["not json", json.dumps(request("ping", request_id=7))])
        self.assertEqual(responses[0]["error"]["code"], mcp_server.PARSE_ERROR)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(responses[1]["id"], 7)

    def test_batch_requests_are_rejected_cleanly(self):
        responses = self.run_serve([json.dumps([request("ping")])])
        self.assertEqual(responses[0]["error"]["code"], mcp_server.INVALID_REQUEST)

    def test_blank_lines_are_ignored(self):
        responses = self.run_serve(["", "   ", json.dumps(request("ping"))])
        self.assertEqual(len(responses), 1)

    def test_responses_are_single_line_json(self):
        server = mcp_server.Server(market_data.FixtureStore(), log=False)
        out = io.StringIO()
        mcp_server.serve(server, io.StringIO(json.dumps(request("tools/list")) + "\n"), out)
        payload = out.getvalue()
        self.assertTrue(payload.endswith("\n"))
        self.assertEqual(payload.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
