"""MCP server entry point (stdio transport, no tools registered yet)."""

from mcp.server import MCPServer


def main() -> None:
    server = MCPServer(name="jira-mini-mcp")
    server.run()


if __name__ == "__main__":
    main()
