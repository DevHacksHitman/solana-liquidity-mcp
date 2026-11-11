"""Tool-layer tests: schemas, argument validation, rendering, dispatch."""

import unittest

import market_data
import tool_registry


class TestToolSpecs(unittest.TestCase):
    def test_registry_shape(self):
        specs = tool_registry.tool_specs()
        names = [spec["name"] for spec in specs]
        self.assertEqual(
            names,
            [
                "liquidity_snapshot",
                "recent_events",
                "token_activity",
                "list_mints",
                "pipeline_status",
            ],
        )
        for spec in specs:
            self.assertTrue(spec["description"])
            self.assertEqual(spec["inputSchema"]["type"], "object")
            self.assertIs(spec["inputSchema"]["additionalProperties"], False)
            self.assertNotIn("handler", spec)


class TestValidateArguments(unittest.TestCase):
    schema = tool_registry.BY_NAME["recent_events"]["inputSchema"]

    def test_unknown_argument_rejected(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(self.schema, {"nope": 1})

    def test_type_enforced(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(self.schema, {"limit": "three"})

    def test_boolean_is_not_an_integer(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(self.schema, {"limit": True})

    def test_range_enforced(self):
        for bad in (0, 201):
            with self.assertRaises(tool_registry.InvalidArguments):
                tool_registry.validate_arguments(self.schema, {"limit": bad})

    def test_enum_enforced(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(self.schema, {"side": "hodl"})
        self.assertEqual(
            tool_registry.validate_arguments(self.schema, {"side": "sell"}), {"side": "sell"}
        )

    def test_required_enforced(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(
                tool_registry.BY_NAME["token_activity"]["inputSchema"], {}
            )

    def test_none_becomes_empty_object(self):
        self.assertEqual(tool_registry.validate_arguments(self.schema, None), {})
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.validate_arguments(self.schema, ["not", "an", "object"])


class TestDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = market_data.FixtureStore()

    def render(self, name, arguments):
        text, data = tool_registry.call_tool(self.store, name, arguments)
        self.assertIsInstance(text, str)
        self.assertIsInstance(data, dict)
        self.assertTrue(text.strip())
        return text, data

    def test_liquidity_snapshot(self):
        text, data = self.render("liquidity_snapshot", {"window_minutes": 60})
        self.assertEqual(data["events"], 84)
        self.assertIn("| mint | events |", text)
        self.assertEqual(data["largest_gap_seconds"], 95.0)

    def test_recent_events_limit_and_filters(self):
        _, data = self.render("recent_events", {"limit": 3})
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["events"][0]["signature"], "sig-demo-0084")

        _, mints = self.render("recent_events", {"limit": 200, "mint": "MINT-DEMO-A"})
        self.assertGreater(mints["count"], 0)
        self.assertTrue(all(row["mint"] == "MINT-DEMO-A" for row in mints["events"]))

        _, sells = self.render("recent_events", {"limit": 5, "side": "sell"})
        self.assertTrue(all(row["side"] == "sell" for row in sells["events"]))

    def test_token_activity_known_and_unknown(self):
        text, data = self.render("token_activity", {"mint": "MINT-DEMO-B", "window_minutes": 60})
        self.assertTrue(data["seen"])
        self.assertIn("MINT-DEMO-B", text)

        text, data = self.render("token_activity", {"mint": "MINT-DOES-NOT-EXIST"})
        self.assertFalse(data["seen"])
        self.assertIn("never been observed", text)

    def test_list_mints(self):
        text, data = self.render("list_mints", {"limit": 2})
        self.assertEqual(len(data["mints"]), 2)
        self.assertIn("| mint | events | last seen |", text)

    def test_pipeline_status(self):
        text, data = self.render("pipeline_status", {})
        self.assertEqual(data["store_kind"], "demo-fixture")
        self.assertEqual(data["dedupe"]["duplicates"], 0)
        self.assertIn("Dedupe", text)

    def test_unknown_tool_raises(self):
        with self.assertRaises(tool_registry.UnknownTool):
            tool_registry.call_tool(self.store, "does_not_exist", {})

    def test_invalid_arguments_raise(self):
        with self.assertRaises(tool_registry.InvalidArguments):
            tool_registry.call_tool(self.store, "recent_events", {"limit": 10_000})


if __name__ == "__main__":
    unittest.main()
