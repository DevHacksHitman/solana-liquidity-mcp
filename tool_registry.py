"""Tool definitions for the MCP server: JSON schemas, validation, rendering.

Each tool is a pure function of ``(store, arguments)`` -> ``(markdown, data)``.
The markdown is for the model to read; ``data`` is returned as
``structuredContent`` for clients that consume typed output.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

import market_data

WINDOW_SCHEMA = {
    "type": "integer",
    "minimum": 1,
    "maximum": 1440,
    "description": "Look-back window in minutes, measured from the newest stored event.",
}


class InvalidArguments(ValueError):
    """Raised when a tool call's arguments fail schema validation."""


class UnknownTool(KeyError):
    """Raised when a tool name is not in the registry."""


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validation (the subset these tools declare)
# ---------------------------------------------------------------------------
_PY_TYPES = {"integer": int, "number": (int, float), "string": str, "boolean": bool}


def validate_arguments(schema: Dict[str, Any], arguments: Any) -> Dict[str, Any]:
    """Validate the declared subset: type, required, range, enum, no unknowns."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidArguments("arguments must be a JSON object")

    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise InvalidArguments(f"unknown argument(s): {', '.join(unknown)}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise InvalidArguments(f"missing required argument: {name}")

    for name, value in arguments.items():
        spec = properties.get(name) or {}
        expected = spec.get("type")
        if expected in _PY_TYPES:
            if isinstance(value, bool) and expected in ("integer", "number"):
                raise InvalidArguments(f"{name} must be {expected}, not boolean")
            if not isinstance(value, _PY_TYPES[expected]):
                raise InvalidArguments(f"{name} must be {expected}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise InvalidArguments(f"{name} must be >= {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                raise InvalidArguments(f"{name} must be <= {spec['maximum']}")
        if "enum" in spec and value not in spec["enum"]:
            allowed = ", ".join(str(item) for item in spec["enum"])
            raise InvalidArguments(f"{name} must be one of: {allowed}")
    return dict(arguments)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def _table(headers: List[str], rows: List[List[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _render_snapshot(data: Dict[str, Any]) -> str:
    if data["events"] == 0:
        return f"No events in the last {data['window_minutes']} minutes."
    gap = data["largest_gap_seconds"]
    header = (
        f"**{data['events']} events** across **{data['distinct_mints']} mints** "
        f"in the last {data['window_minutes']} min"
        + (f" (~{data['events_per_minute']}/min)" if data["events_per_minute"] else "")
    )
    parts = [
        header,
        "",
        f"Window: {data['first_event_at']} → {data['last_event_at']}",
        f"Sides: {data['sides'] or '—'}",
        f"Largest inter-arrival gap: {_fmt(gap)}s" if gap is not None else "No gaps measured.",
        "",
        _table(
            ["mint", "events"],
            [[row["mint"], row["events"]] for row in data["top_mints"]],
        ),
    ]
    return "\n".join(parts)


def _render_events(data: Dict[str, Any]) -> str:
    if not data["events"]:
        return "No matching events."
    rows = [
        [
            event["observed_at"],
            event["side"] or "—",
            event["mint"] or "—",
            event["signature"],
        ]
        for event in data["events"]
    ]
    return "\n".join(
        [
            f"{data['count']} event(s), newest first.",
            "",
            _table(["time (UTC)", "side", "mint", "signature"], rows),
        ]
    )


def _render_mint(data: Dict[str, Any]) -> str:
    if not data["seen"]:
        return f"Mint `{data['mint']}` has never been observed in this store."
    lines = [
        f"**{data['mint']}** — {data['in_window']} event(s) in the last "
        f"{data['window_minutes']} min ({data['events_total']} total).",
        "",
        f"Sides: {data['sides'] or '—'}",
        f"First seen: {data['first_seen_at']}",
        f"Last seen: {data['last_seen_at']}",
    ]
    if data["avg_interval_seconds"] is not None:
        lines.append(f"Mean interval between events: {data['avg_interval_seconds']}s")
    if data["in_window"] == 0:
        lines.append("\n_Quiet in this window — check `list_mints` for alternatives._")
    return "\n".join(lines)


def _render_mints(data: Dict[str, Any]) -> str:
    if not data["mints"]:
        return "No mints observed."
    return "\n".join(
        [
            f"{len(data['mints'])} mint(s), ranked by event count.",
            "",
            _table(
                ["mint", "events", "last seen"],
                [
                    [row["mint"], row["events"], row["last_seen_at"]]
                    for row in data["mints"]
                ],
            ),
        ]
    )


def _render_status(data: Dict[str, Any]) -> str:
    dedupe = data["dedupe"]
    return "\n".join(
        [
            f"**Store:** {data['store']}",
            f"**Rows:** {data['events_total']} · **span:** {_fmt(data['span_minutes'])} min",
            f"**Range:** {data['first_event_at']} → {data['newest_event_at']}",
            f"**Dedupe:** {dedupe['unique_signatures']}/{dedupe['rows']} unique signatures, "
            f"{dedupe['duplicates']} duplicate(s)",
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _liquidity_snapshot(store: Any, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    data = market_data.snapshot(store.events, arguments.get("window_minutes", 60))
    return _render_snapshot(data), data


def _recent_events(store: Any, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    rows = market_data.window_slice(store.events, arguments.get("window_minutes"))
    if arguments.get("mint"):
        rows = [row for row in rows if row["mint"] == arguments["mint"]]
    if arguments.get("side"):
        rows = [row for row in rows if row["side"] == arguments["side"]]
    limit = arguments.get("limit", 20)
    selected = rows[:limit]
    data = {
        "count": len(selected),
        "matching_total": len(rows),
        "events": [
            {
                "observed_at": market_data.iso(row["observed_at"]),
                "side": row["side"],
                "mint": row["mint"],
                "signature": row["signature"],
                "slot": row["slot"],
            }
            for row in selected
        ],
    }
    return _render_events(data), data


def _token_activity(store: Any, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    data = market_data.mint_activity(
        store.events, arguments["mint"], arguments.get("window_minutes", 60)
    )
    return _render_mint(data), data


def _list_mints(store: Any, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    rows = market_data.list_mints(
        store.events, arguments.get("limit", 10), arguments.get("window_minutes")
    )
    return _render_mints({"mints": rows}), {"mints": rows}


def _pipeline_status(store: Any, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    data = market_data.status(store)
    return _render_status(data), data


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "liquidity_snapshot",
        "title": "Liquidity snapshot",
        "description": (
            "Aggregate market activity over a look-back window: event count, "
            "events per minute, distinct mints, buy/sell split, the busiest mints, "
            "and the largest inter-arrival gap (a pipeline-health signal)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"window_minutes": WINDOW_SCHEMA},
            "additionalProperties": False,
        },
        "handler": _liquidity_snapshot,
    },
    {
        "name": "recent_events",
        "title": "Recent events",
        "description": (
            "List the newest observed trade events, optionally filtered by mint "
            "and/or side. Each row carries the timestamp, side, mint and "
            "transaction signature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum rows to return.",
                },
                "mint": {"type": "string", "description": "Filter to one mint."},
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "Filter by trade side.",
                },
            },
            "additionalProperties": False,
        },
        "handler": _recent_events,
    },
    {
        "name": "token_activity",
        "title": "Token activity",
        "description": (
            "Activity for a single mint: event counts by side, first/last seen, "
            "and the mean interval between events. Distinguishes a mint that was "
            "never observed from one that is merely quiet in the window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mint": {"type": "string", "description": "The mint to inspect."},
                "window_minutes": WINDOW_SCHEMA,
            },
            "required": ["mint"],
            "additionalProperties": False,
        },
        "handler": _token_activity,
    },
    {
        "name": "list_mints",
        "title": "List mints",
        "description": "Mints ranked by event count, with last-seen timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum rows to return.",
                },
                "window_minutes": dict(WINDOW_SCHEMA, description=(
                    "Restrict to events within this many minutes of the newest "
                    "stored event. Omit for all history."
                )),
            },
            "additionalProperties": False,
        },
        "handler": _list_mints,
    },
    {
        "name": "pipeline_status",
        "title": "Pipeline status",
        "description": (
            "Health of the connected data store: source description, row count, "
            "time span, and a signature-uniqueness audit (duplicate detection)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _pipeline_status,
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def tool_specs() -> List[Dict[str, Any]]:
    """The wire-facing tool list (no handler callables)."""
    return [
        {
            "name": tool["name"],
            "title": tool["title"],
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
        }
        for tool in TOOLS
    ]


def call_tool(store: Any, name: str, arguments: Any) -> Tuple[str, Dict[str, Any]]:
    """Validate and execute one tool call."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise UnknownTool(name)
    clean = validate_arguments(tool["inputSchema"], arguments)
    return tool["handler"](store, clean)
