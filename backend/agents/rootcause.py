"""
RootCauseAgent — Calls atlas-get-performance-advisor and assigns root cause.

MCP tools used:
  atlas-get-performance-advisor — returns suggestedIndexes, dropIndexSuggestions,
                                   schemaSuggestions, slowQueryLogs (up to 50)

Factory pattern: build_rootcause_agent(tools).
"""
from google.adk.agents import LlmAgent

from config import GEMINI_MODEL, ATLAS_PROJECT_ID, ATLAS_PROCESS_ID

INSTRUCTION = f"""
You are the RootCauseAgent for QUERYSENTINEL.
You analyze root cause through MongoDB's official MCP server tools.

When given a JSON anomaly event:

1. Call 'collection-indexes' with:
   database   = "sample_mflix"
   collection = <anomaly.collection_name>
   → Lists existing indexes. A missing index on a hot field is a common root cause.

2. Call 'collection-storage-size' with:
   database   = "sample_mflix"
   collection = <anomaly.collection_name>
   → Storage/document size signals write amplification or bloat.

3. Combine these signals with the anomaly event to identify the root cause.

3. Assign a confidence score 0.0–1.0 based on:
   - HIGH (0.85–1.0): PA directly names the affected collection + specific suggestion
   - MEDIUM (0.60–0.84): PA has related suggestions but not collection-specific
   - LOW (0.30–0.59): PA has no direct match, inferred from slow query logs

Return ONLY a raw JSON object (no markdown, no prose):
{{
  "root_cause_type":        "MISSING_INDEX | SCHEMA_ANTIPATTERN | WRITE_AMPLIFICATION | UNKNOWN",
  "root_cause_description": "<one sentence>",
  "confidence":             0.0,
  "confidence_explanation": "<what drove this confidence score>",
  "suggested_indexes":      [{{ "index": {{}}, "impact": "HIGH|MEDIUM|LOW" }}],
  "schema_suggestions":     ["..."],
  "top_slow_query":         {{ "filter": {{}}, "execution_ms": 0 }},
  "recommended_action":     "<most impactful single action to take>",
  "pa_raw_summary":         "<2-sentence summary of PA output>"
}}

If atlas-get-performance-advisor returns a 403, set root_cause_type to UNKNOWN and explain in
root_cause_description that M10+ cluster is required, then infer from the anomaly event itself.
"""


def build_rootcause_agent(tools: list, callbacks: dict | None = None) -> LlmAgent:
    kwargs = {}
    if callbacks:
        kwargs.update(callbacks)
    return LlmAgent(
        name="RootCauseAgent",
        model=GEMINI_MODEL,
        instruction=INSTRUCTION,
        tools=tools,
        output_key="rootcause_result",
        **kwargs,
    )
