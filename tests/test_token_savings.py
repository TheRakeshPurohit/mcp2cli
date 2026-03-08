"""Tests that measure and verify LLM token savings from using mcp2cli.

Compares the native tool injection approach (all tool schemas in the system
prompt on every turn) vs mcp2cli's on-demand CLI approach, across realistic
multi-turn conversations.
"""

import json
import subprocess
import sys
from pathlib import Path

import tiktoken

from conftest import PETSTORE_SPEC

enc = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(enc.encode(text))


# ---------------------------------------------------------------------------
# Realistic tool schemas — what the LLM API actually injects
# ---------------------------------------------------------------------------

def _build_native_tool_definitions(tools: list[dict]) -> list[dict]:
    """Build the tool definitions that an LLM API injects into the system prompt.

    This mirrors the Claude/OpenAI tool_use format: each tool has name,
    description, and a full JSON Schema for input_schema.
    """
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {}),
        }
        for t in tools
    ]


def _openapi_to_native_tools(spec: dict) -> list[dict]:
    """Convert an OpenAPI spec to the native tool injection format.

    Each operation becomes a tool with its full parameter/body schema.
    """
    tools = []
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            properties = {}
            required = []

            for p in op.get("parameters", []):
                prop = dict(p.get("schema", {}))
                prop["description"] = p.get("description", "")
                properties[p["name"]] = prop
                if p.get("required"):
                    required.append(p["name"])

            body = op.get("requestBody", {})
            body_schema = (
                body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if body_schema:
                for prop_name, prop_schema in body_schema.get("properties", {}).items():
                    properties[prop_name] = prop_schema
                    if prop_name in body_schema.get("required", []):
                        required.append(prop_name)

            tools.append({
                "name": op.get("operationId", f"{method}_{path}"),
                "description": op.get("summary", ""),
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    **({"required": required} if required else {}),
                },
            })
    return tools


# The MCP test server's tool definitions
MCP_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "add_numbers",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First number"},
                "b": {"type": "integer", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "list_items",
        "description": "List items in a directory (test tool)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "recursive": {"type": "boolean", "description": "Recurse into subdirs"},
            },
            "required": ["path"],
        },
    },
]

# The one-line system prompt mcp2cli needs
MCP2CLI_SYSTEM_PROMPT = (
    'Use `mcp2cli --spec <url> <command> [--flags]` to interact with the API. '
    'Run `mcp2cli --spec <url> --list` to see available commands, '
    'or `mcp2cli --spec <url> <command> --help` for details on a specific command.'
)


def _simulate_conversation(
    num_turns: int,
    native_tool_tokens: int,
    mcp2cli_prompt_tokens: int,
    discovery_tokens: int,
    num_tool_calls: int,
    tool_call_output_tokens: int,
) -> dict:
    """Simulate token costs over a multi-turn conversation.

    Native: tool definitions are injected on EVERY turn.
    mcp2cli: system prompt is injected on every turn, discovery happens once,
    tool calls add their output to the context.

    Returns token counts and savings for both approaches.
    """
    # Native: pay the full tool schema cost every turn, plus tool call outputs
    native_total = (native_tool_tokens * num_turns) + (tool_call_output_tokens * num_tool_calls)

    # mcp2cli: pay the small system prompt every turn, discovery once, tool call outputs
    mcp2cli_total = (
        (mcp2cli_prompt_tokens * num_turns)
        + discovery_tokens  # --list, run once
        + (tool_call_output_tokens * num_tool_calls)
    )

    return {
        "turns": num_turns,
        "tool_calls": num_tool_calls,
        "native_total": native_total,
        "mcp2cli_total": mcp2cli_total,
        "tokens_saved": native_total - mcp2cli_total,
        "reduction_pct": round((1 - mcp2cli_total / native_total) * 100, 1),
    }


class TestTokenSavings:
    """Measure and verify token savings from mcp2cli vs native tool injection."""

    def test_system_prompt_token_count(self):
        """The mcp2cli system prompt should be under 100 tokens."""
        tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)
        assert tokens < 100, f"System prompt is {tokens} tokens, expected < 100"
        print(f"\nmcp2cli system prompt: {tokens} tokens")

    def test_petstore_openapi_savings(self):
        """Measure savings for a 5-endpoint petstore API."""
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        native_text = json.dumps(native_tools)
        native_tokens = _count_tokens(native_text)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # Simulate --list output
        list_output = "list-pets  create-pet  get-pet  delete-pet  update-pet"
        list_tokens = _count_tokens(list_output)

        # Average tool call output: ~30 tokens
        call_output_tokens = 30

        result = _simulate_conversation(
            num_turns=10,
            native_tool_tokens=native_tokens,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=5,
            tool_call_output_tokens=call_output_tokens,
        )

        print(f"\n--- Petstore (5 endpoints) over {result['turns']} turns ---")
        print(f"Native tool injection:  {native_tokens} tokens/turn")
        print(f"mcp2cli system prompt:  {prompt_tokens} tokens/turn")
        print(f"Discovery (--list):     {list_tokens} tokens (once)")
        print(f"Native total:           {result['native_total']:,} tokens")
        print(f"mcp2cli total:          {result['mcp2cli_total']:,} tokens")
        print(f"Tokens saved:           {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 70, f"Expected >70% reduction, got {result['reduction_pct']}%"

    def test_mcp_server_savings(self):
        """Measure savings for our 3-tool MCP test server."""
        native_tools = _build_native_tool_definitions(MCP_TOOLS)
        native_text = json.dumps(native_tools)
        native_tokens = _count_tokens(native_text)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        list_output = "echo  add-numbers  list-items"
        list_tokens = _count_tokens(list_output)

        result = _simulate_conversation(
            num_turns=10,
            native_tool_tokens=native_tokens,
            mcp2cli_prompt_tokens=prompt_tokens,
            discovery_tokens=list_tokens,
            num_tool_calls=5,
            tool_call_output_tokens=20,
        )

        print(f"\n--- MCP server (3 tools) over {result['turns']} turns ---")
        print(f"Native tool injection:  {native_tokens} tokens/turn")
        print(f"mcp2cli system prompt:  {prompt_tokens} tokens/turn")
        print(f"Discovery (--list):     {list_tokens} tokens (once)")
        print(f"Native total:           {result['native_total']:,} tokens")
        print(f"mcp2cli total:          {result['mcp2cli_total']:,} tokens")
        print(f"Tokens saved:           {result['tokens_saved']:,} ({result['reduction_pct']}%)")

        assert result["reduction_pct"] > 50, f"Expected >50% reduction, got {result['reduction_pct']}%"

    def test_scaled_api_savings(self):
        """Project savings for realistic API sizes: 20, 50, 200 endpoints.

        Uses per-endpoint token cost from our petstore spec to extrapolate.
        """
        # Measure per-endpoint cost from petstore
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        scenarios = [
            {"name": "Medium API", "endpoints": 20, "turns": 15, "calls": 8},
            {"name": "Large API", "endpoints": 50, "turns": 20, "calls": 12},
            {"name": "Enterprise API", "endpoints": 200, "turns": 25, "calls": 15},
        ]

        print(f"\nPer-endpoint token cost: {per_endpoint:.0f} tokens")
        print(f"mcp2cli prompt: {prompt_tokens} tokens/turn")
        print()

        for s in scenarios:
            native_tokens = int(per_endpoint * s["endpoints"])
            # --list output: ~3 tokens per command name
            list_tokens = s["endpoints"] * 3

            result = _simulate_conversation(
                num_turns=s["turns"],
                native_tool_tokens=native_tokens,
                mcp2cli_prompt_tokens=prompt_tokens,
                discovery_tokens=list_tokens,
                num_tool_calls=s["calls"],
                tool_call_output_tokens=30,
            )

            print(f"--- {s['name']} ({s['endpoints']} endpoints) over {result['turns']} turns, {s['calls']} calls ---")
            print(f"  Native: {result['native_total']:>8,} tokens")
            print(f"  mcp2cli: {result['mcp2cli_total']:>7,} tokens")
            print(f"  Saved: {result['tokens_saved']:>9,} tokens ({result['reduction_pct']}%)")

            assert result["reduction_pct"] > 90, (
                f"{s['name']}: expected >90% reduction, got {result['reduction_pct']}%"
            )

    def test_conversation_breakdown(self):
        """Show turn-by-turn token accumulation for a concrete scenario.

        This is the detailed version that demonstrates exactly where
        tokens are spent in a 10-turn conversation with a 50-endpoint API.
        """
        endpoints = 50
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        native_per_turn = int(per_endpoint * endpoints)
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)
        list_tokens = endpoints * 3  # ~3 tokens per command name

        turns = 10
        # Simulate: turn 1 = discovery, turns 3,5,7,9 = tool calls
        tool_call_turns = {3, 5, 7, 9}
        call_output = 30

        native_cumulative = 0
        mcp2cli_cumulative = 0

        print(f"\n{'Turn':<6} {'Native':<12} {'mcp2cli':<12} {'Savings':<12}")
        print("-" * 42)

        for turn in range(1, turns + 1):
            # Native: always pay full tool schemas
            native_cumulative += native_per_turn
            # mcp2cli: always pay small prompt
            mcp2cli_cumulative += prompt_tokens

            if turn == 1:
                # Discovery turn for mcp2cli
                mcp2cli_cumulative += list_tokens

            if turn in tool_call_turns:
                # Both pay for tool call output
                native_cumulative += call_output
                mcp2cli_cumulative += call_output

            savings = native_cumulative - mcp2cli_cumulative
            print(f"{turn:<6} {native_cumulative:<12,} {mcp2cli_cumulative:<12,} {savings:<12,}")

        pct = round((1 - mcp2cli_cumulative / native_cumulative) * 100, 1)
        print(f"\nTotal savings: {native_cumulative - mcp2cli_cumulative:,} tokens ({pct}%)")

        assert pct > 95, f"Expected >95% savings over {turns} turns, got {pct}%"

    def test_actual_cli_list_output_tokens(self):
        """Measure the actual token cost of running mcp2cli --list."""
        spec_file = Path(__file__).parent / "_petstore_tmp.json"
        spec_file.write_text(json.dumps(PETSTORE_SPEC))
        try:
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--spec", str(spec_file),
                 "--base-url", "http://unused", "--list"],
                capture_output=True, text=True, timeout=15,
            )
            assert r.returncode == 0
            list_output = r.stdout.strip()
            list_tokens = _count_tokens(list_output)

            native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
            native_tokens = _count_tokens(json.dumps(native_tools))

            print(f"\n--- Actual CLI output ---")
            print(f"--list output ({list_tokens} tokens):")
            for line in list_output.split("\n"):
                print(f"  {line}")
            print(f"\nNative tool schemas: {native_tokens} tokens")
            print(f"--list output: {list_tokens} tokens")
            print(f"Ratio: {native_tokens / max(list_tokens, 1):.1f}x more compact")

            assert list_tokens < native_tokens, "CLI list should be more compact than full schemas"
        finally:
            spec_file.unlink(missing_ok=True)

    def test_help_output_tokens(self):
        """Measure token cost of a single command's --help vs its native schema."""
        spec_file = Path(__file__).parent / "_petstore_tmp.json"
        spec_file.write_text(json.dumps(PETSTORE_SPEC))
        try:
            r = subprocess.run(
                [sys.executable, "-m", "mcp2cli", "--spec", str(spec_file),
                 "--base-url", "http://unused", "create-pet", "--help"],
                capture_output=True, text=True, timeout=15,
            )
            assert r.returncode == 0
            help_output = r.stdout.strip()
            help_tokens = _count_tokens(help_output)

            # Native schema for create-pet
            native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
            create_tool = next(t for t in native_tools if t["name"] == "createPet")
            native_tokens = _count_tokens(json.dumps(create_tool))

            print(f"\n--- create-pet --help vs native schema ---")
            print(f"--help output: {help_tokens} tokens")
            print(f"Native schema: {native_tokens} tokens")
            print(f"Difference: {help_tokens - native_tokens:+d} tokens")
            print(f"(--help is loaded once on demand, not every turn)")
        finally:
            spec_file.unlink(missing_ok=True)

    def test_readme_numbers_are_accurate(self):
        """Verify the specific numbers claimed in the README are grounded in measurement."""
        prompt_tokens = _count_tokens(MCP2CLI_SYSTEM_PROMPT)

        # System prompt should be compact
        assert 40 <= prompt_tokens <= 80, (
            f"System prompt is {prompt_tokens} tokens, expected 40-80"
        )

        # Per-endpoint cost should be meaningful
        native_tools = _openapi_to_native_tools(PETSTORE_SPEC)
        per_endpoint = _count_tokens(json.dumps(native_tools)) / len(native_tools)
        assert per_endpoint > 30, f"Per-endpoint cost is {per_endpoint}, expected > 30"

        # The savings ratio should hold: mcp2cli prompt < any meaningful tool set
        native_5 = _count_tokens(json.dumps(native_tools))
        assert prompt_tokens < native_5, (
            f"System prompt ({prompt_tokens}) should be less than 5-endpoint schemas ({native_5})"
        )

        print(f"\nMeasured numbers:")
        print(f"  System prompt: {prompt_tokens} tokens")
        print(f"  Per-endpoint cost: {per_endpoint:.0f} tokens")
        print(f"  5 endpoints native: {native_5} tokens")
        print(f"  Ratio: {native_5 / prompt_tokens:.1f}x")
