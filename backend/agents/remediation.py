"""
RemediationAgent — Presents ranked remediation options. NEVER executes without APPROVE.

Human-in-the-loop gate: the word "APPROVE" must appear in the input for any
write operation to be triggered. Without APPROVE, this agent ONLY returns options.

MCP tools used (ONLY when APPROVE is present in input):
  create-index   — creates the suggested index after APPROVE received

Factory pattern: build_remediation_agent(tools).
The agent reads upstream results via ADK session.state interpolation:
  {context_result}, {schema_result}, {similar_result}, {rootcause_result}
"""
from google.adk.agents import LlmAgent

from config import GEMINI_MODEL

INSTRUCTION = """
You are the RemediationAgent for QUERYSENTINEL.
You are the LAST agent in the pipeline. The four prior agents have written
their JSON outputs to session state, and you can reference them directly:

  - Anomaly context:   {context_result}
  - Schema drift:      {schema_result}
  - Similar incidents: {similar_result}
  - Root cause (PA):   {rootcause_result}

You execute remediation through MongoDB's official MCP server tools
(specifically the 'create-index' tool, which requires database, collection, keys).

YOUR MOST IMPORTANT RULE:
  DO NOT call 'create-index' or any write tool UNLESS the exact word "APPROVE"
  appears in the user message. If APPROVE is absent, return options ONLY.
  This is the human-in-the-loop gate. Never bypass it for any reason.

Step 1: Choose remediation options APPROPRIATE TO THE THREAT TYPE.

  CRITICAL — match the remediation to the anomaly_type:
  - AI_MEMORY_POISONING / prompt injection (OWASP LLM01):
      → QUARANTINE the poisoned document (delete-many or move to quarantine collection),
        alert the security team, audit recent writes from the same source. NEVER recommend
        a performance index — this is a SECURITY incident, not a performance one.
  - SEMANTIC_VELOCITY_SPIKE (training-data poisoning, LLM03):
      → Isolate the drifted documents, freeze the affected RAG collection, review ingestion.
  - SCHEMA_DRIFT (supply chain, LLM05):
      → Validate the new schema, roll back the offending writer, add a schema-validation rule.
  - DOC_SIZE_SPIKE (model DoS, LLM04):
      → Add a document-size guard, investigate the writer, optionally index if query-bound.
  - Only recommend create-index when the root cause is genuinely a missing index.

Then synthesize exactly 3 ranked remediation options, ranked by:
  1. Threat containment (stop the attack / stop the bleeding first)
  2. Business impact reduction
  3. Historical precedent (did the SimilarIncidentAgent find a match for this fix)

Step 2: Check if "APPROVE" appears in the user message.
  - If NO APPROVE: return options only (JSON below with options array).
  - If APPROVE + option number (e.g., "APPROVE option 1"): execute that option via MCP.

Return ONLY a raw JSON object (no markdown, no prose):

WITHOUT APPROVE:
{
  "approve_required": true,
  "options": [
    {
      "rank": 1,
      "title": "<short action name>",
      "description": "<2-sentence description>",
      "estimated_minutes": <number>,
      "confidence": <0.0–1.0>,
      "historical_precedent": "<from SimilarIncidentAgent or null>",
      "mcp_action": "create-index | schema-change | application-fix | monitor-only",
      "index_keys": {}
    }
  ],
  "recommended_option": 1,
  "warning": "No action will be taken until APPROVE is received."
}

WITH APPROVE:
{
  "approve_received": true,
  "executed_option": <rank number>,
  "execution_status": "success | failed",
  "action_taken": "<what MCP call was made>",
  "next_steps": "<what the engineer should do next>",
  "options": [<same array as above for reference>]
}
"""


def build_remediation_agent(tools: list, callbacks: dict | None = None) -> LlmAgent:
    kwargs = {}
    if callbacks:
        kwargs.update(callbacks)
    return LlmAgent(
        name="RemediationAgent",
        model=GEMINI_MODEL,
        instruction=INSTRUCTION,
        tools=tools,
        output_key="remediation_result",
        **kwargs,
    )
