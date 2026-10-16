"""Run the local service. Explicit data directory avoids CWD-dependent storage."""

import argparse
import os

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Sijing local knowledge service")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=47831)
    parser.add_argument("--mcp", action="store_true", help="Run the stdio MCP server")
    args = parser.parse_args()
    if args.mcp:
        from .mcp_server import run
        run(args.data_dir)
        return
    token = os.environ.get("BUSINESSMAP_TOKEN", "")
    if len(token) < 16:
        parser.error("BUSINESSMAP_TOKEN must contain at least 16 characters")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(create_app(args.data_dir, token), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
