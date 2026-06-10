"""
config.py — Central settings for QUERYSENTINEL backend.
All environment variables loaded once here; imported everywhere else.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── MongoDB ──────────────────────────────────────────────────────────────────
MONGODB_URI        = os.environ["MONGODB_URI"]
ATLAS_CLIENT_ID    = os.environ.get("ATLAS_CLIENT_ID", "")
ATLAS_CLIENT_SECRET= os.environ.get("ATLAS_CLIENT_SECRET", "")
ATLAS_PROJECT_ID   = os.environ.get("ATLAS_PROJECT_ID", "")
ATLAS_PROCESS_ID   = os.environ.get("ATLAS_CLUSTER_PROCESS_ID", "")

APP_DB             = os.getenv("APP_DB", "querysentinel")
SOURCE_DB          = os.getenv("SOURCE_DB", "sample_mflix")
MONITORED_COLLECTIONS = os.getenv(
    "MONITORED_COLLECTIONS", "movies,comments,users,sessions"
).split(",")

# ── Detection thresholds ─────────────────────────────────────────────────────
ANOMALY_Z    = float(os.getenv("ANOMALY_THRESHOLD_Z",  "2.0"))
CRITICAL_Z   = float(os.getenv("CRITICAL_THRESHOLD_Z", "3.5"))

# ── Google Cloud ─────────────────────────────────────────────────────────────
GOOGLE_PROJECT  = os.getenv("GOOGLE_PROJECT_ID", "")
GOOGLE_REGION   = os.getenv("GOOGLE_REGION", "us-central1")

# ── LLM Backend (single source of truth for all agents) ──────────────────────
# LLM_BACKEND controls the model engine:
#
#   gemini (default) → Google Gemini (gemini-3.5-flash) via Google ADK.
#                       GOOGLE_API_KEYS required. This is the submission engine.
#
#   none             → Deterministic detection-only mode. The pipeline produces
#                       complete incidents from detection signals (regex /
#                       semantic velocity / vector search / schema) with NO model
#                       calls — the resilience layer so detection never depends on
#                       an external API being available.
#
# (Internal OpenAI-compatible fallbacks exist for offline dev only; gemini is the
#  engine for the hackathon submission.)

_llm_backend = os.getenv("LLM_BACKEND", "gemini").lower()

if _llm_backend in ("none", "off", "deterministic"):
    GEMINI_MODEL = "none"

elif _llm_backend in ("nvidia", "ollama"):
    # Offline dev fallbacks (OpenAI-compatible). Not used for submission.
    try:
        from google.adk.models.lite_llm import LiteLlm as _LiteLlm
        import litellm as _litellm
        _litellm.AIOHTTP_ENABLED = False
        if _llm_backend == "ollama":
            GEMINI_MODEL = f"openai/{os.getenv('OLLAMA_MODEL', 'llama3.2:3b')}"
        else:
            GEMINI_MODEL = _LiteLlm(
                model=f"openai/{os.getenv('NVIDIA_MODEL', 'meta/llama-3.1-8b-instruct')}",
                api_base="https://integrate.api.nvidia.com/v1",
                api_key=os.getenv("NVIDIA_API_KEY", ""),
            )
    except Exception:
        GEMINI_MODEL = "none"

else:
    # Default: Gemini — the hackathon submission engine.
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# ── MongoDB MCP Server integration (HACKATHON COMPLIANCE) ────────────────────
# The Google Cloud Rapid Agent Hackathon REQUIRES: "integrates a Partner Entity's
# MCP server." For the MongoDB track, that means MongoDB's official MCP server
# (npx mongodb-mcp-server, 29 tools). When GEMINI_USE_MCP=true (default), the
# Gemini pipeline runs all agents through MongoDB's MCP server via ADK MCPToolset
# in a SequentialAgent (MCPToolset is incompatible with ParallelAgent's anyio
# cancel scopes — sequential execution avoids that).
#
# Set GEMINI_USE_MCP=false ONLY as an emergency fallback to the direct-pymongo
# FunctionTools + ParallelAgent path (faster, but NOT partner-MCP-compliant).
GEMINI_USE_MCP = os.getenv("GEMINI_USE_MCP", "true").lower() in ("true", "1", "yes")

# ── Atlas Stream Processing ──────────────────────────────────────────────────
ASP_INSTANCE_NAME = os.getenv("ASP_INSTANCE_NAME", "querysentinel-asp")
ASP_PROCESSOR_NAME = os.getenv("ASP_PROCESSOR_NAME", "qs-rolling-stats")
ASP_CONNECTION_NAME = os.getenv("ASP_CONNECTION_NAME", "atlasConnection")

# ── App ───────────────────────────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8000"))
