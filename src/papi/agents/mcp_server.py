"""Stdio MCP server for one agent: ``python -m papi.agents.mcp_server <agent>``.

Serves exactly the agent's bound tools plus ``use_skill`` / ``read_skill_file``. The claude CLI is
started with ``--strict-mcp-config`` and ``--tools ""``, so nothing else is reachable.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from papi.agents.definition import AgentDef, load_agent
from papi.agents.registry import guard
from papi.agents.skills import SkillError

SERVER_NAME = "papi"


def build_server(agent: AgentDef) -> MCPServer:
    server = MCPServer(SERVER_NAME)

    def use_skill(name: str) -> dict[str, Any]:
        """Load a skill: returns its instructions, the tools it binds and its reference files."""
        skill = agent.skill(name)
        if skill is None:
            return {
                "success": False,
                "error": f"unknown skill {name!r}; available: {[s.name for s in agent.skills]}",
            }
        return {
            "success": True,
            "name": skill.name,
            "instructions": skill.body,
            "tools": list(skill.allowed_tools),
            "files": skill.files(),
        }

    def read_skill_file(name: str, path: str) -> dict[str, Any]:
        """Read one reference file of a skill (path as listed by use_skill, e.g. references/x.md)."""
        skill = agent.skill(name)
        if skill is None:
            return {"success": False, "error": f"unknown skill {name!r}"}
        try:
            return {"success": True, "path": path, "content": skill.read_file(path)}
        except SkillError as exc:
            return {"success": False, "error": str(exc)}

    server.add_tool(guard("use_skill", use_skill, max_chars=110_000), name="use_skill")
    server.add_tool(guard("read_skill_file", read_skill_file, max_chars=110_000), name="read_skill_file")
    for tool in agent.tools:
        server.add_tool(
            guard(tool.name, tool.fn, max_chars=tool.max_chars), name=tool.name, description=tool.description
        )
    return server


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit("usage: python -m papi.agents.mcp_server <agent>")
    build_server(load_agent(args[0])).run("stdio")


if __name__ == "__main__":
    main()
