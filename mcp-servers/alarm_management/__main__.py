"""`python -m alarm_management` — run the MCP server on its own.

Independently runnable is a requirement, not a convenience: the server has to be startable
and inspectable without the copilot, both for the demo and so that a failure can be
attributed to one side of the boundary.

Two transports from one codebase. `stdio` is what an MCP-aware desktop client spawns;
`streamable-http` is what the copilot backend dials (`MCP_SERVER_URL`) and what makes the
server curl-able. The flags default to the environment so `make mcp` and a hand-typed
command behave identically.
"""

from __future__ import annotations

import argparse
import sys

from alarm_management.config import Transport, get_settings
from alarm_management.server import SERVER_NAME, build_server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="alarm_management",
        description="MCP server exposing a plant alarm management system as tools.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=settings.transport,
        help="stdio for a desktop MCP client, streamable-http to serve over the network",
    )
    parser.add_argument("--host", default=settings.host, help="streamable-http bind address")
    parser.add_argument("--port", type=int, default=settings.port, help="streamable-http port")
    parser.add_argument("--path", default=settings.path, help="streamable-http endpoint path")
    parser.add_argument(
        "--alarm-api-url",
        default=settings.alarm_api_base_url,
        help="Base URL of the alarm management API this server fronts",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings().model_copy(
        update={
            "transport": args.transport,
            "host": args.host,
            "port": args.port,
            "path": args.path,
            "alarm_api_base_url": args.alarm_api_url,
        }
    )
    server = build_server(settings)

    transport: Transport = args.transport
    if transport == "stdio":
        # Nothing may be written to stdout on this transport: it is the protocol channel,
        # and a stray print corrupts the session.
        print(f"{SERVER_NAME}: serving over stdio", file=sys.stderr)
        server.run(transport="stdio")
        return 0

    print(
        f"{SERVER_NAME}: serving at http://{settings.host}:{settings.port}{settings.path} "
        f"(alarm API: {settings.alarm_api_base_url})",
        file=sys.stderr,
    )
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
