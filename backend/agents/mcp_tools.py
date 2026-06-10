"""
mcp_tools.py — MongoDB MCP toolset factory for QUERYSENTINEL agents.

Bugs fixed:
  ✓ NO module-level MCPToolset instantiation (would crash on import if npx missing,
    and would attempt to start a subprocess before any event loop exists on Cloud Run)
  ✓ Factory pattern: orchestrator builds ONE shared toolset per pipeline run and
    passes it to all 5 agents — instead of each agent spawning its own npx process
  ✓ Full PATH inheritance so npx is found in all environments (nvm, fnm, Homebrew, Docker)

Usage from orchestrator (inside async function only):
    toolset = make_mongo_toolset()
    tools = [toolset]
    context = build_context_agent(tools)
    ...
"""
import os
import logging
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

from config import MONGODB_URI, ATLAS_CLIENT_ID, ATLAS_CLIENT_SECRET, ATLAS_PROJECT_ID

logger = logging.getLogger("mcp_tools")


def make_mongo_toolset() -> MCPToolset:
    """
    Construct a fresh MCPToolset bound to MongoDB MCP server via npx.

    Important: this MUST be called from inside an async context (a running
    asyncio event loop). The toolset opens the stdio transport lazily on
    first tool invocation, so calling this function does NOT immediately
    spawn npx — but ADK requires a running loop for the lazy lifecycle.

    Each call returns a NEW toolset. The orchestrator builds one per
    pipeline run and reuses it across all 5 agents in that run. Do NOT
    cache at module level — that breaks concurrent pipeline runs.
    """
    env = dict(os.environ)
    env.update({
        "MCP_CONNECTION_STRING":  MONGODB_URI,
        "ATLAS_CLIENT_ID":        ATLAS_CLIENT_ID,
        "ATLAS_CLIENT_SECRET":    ATLAS_CLIENT_SECRET,
        "ATLAS_PROJECT_ID":       ATLAS_PROJECT_ID,
        "CI":                     "true",
        "MCP_QUIET":              "true",
    })

    # StdioConnectionParams wraps the server params with a timeout. The default
    # 5s is too short — MongoDB MCP server's first tool call triggers a lazy Atlas
    # connection that can take 10-20s. 60s gives ample headroom.
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=["-y", "mongodb-mcp-server"],
                env=env,
            ),
            timeout=60.0,
        ),
    )
