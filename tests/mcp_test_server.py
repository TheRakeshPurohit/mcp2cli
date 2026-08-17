"""Minimal MCP server for testing — runs over stdio.

Speaks the JSON-RPC wire protocol directly (see `_mcp_fixture`) so the same
fixture works against any `mcp` SDK major. MCP's stdio transport is
newline-delimited JSON in both directions.
"""

import json
import sys

from _mcp_fixture import handle


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
