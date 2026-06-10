"""
SchemaDriftAgent — Diffs live schema against stored baseline.

MCP tools used:
  collection-schema  — returns current live field paths and types
  find               — retrieves stored baseline from collection_baselines

Factory pattern: build_schema_agent(tools).
"""
from google.adk.agents import LlmAgent

from config import GEMINI_MODEL

INSTRUCTION = """
You are the SchemaDriftAgent for QUERYSENTINEL.

When given a JSON anomaly event with collection_name, do this:

You query MongoDB through MongoDB's official MCP server tools.

1. Call 'collection-schema' with:
   database   = "sample_mflix"
   collection = <anomaly.collection_name>
   → Get the current live schema (all field paths and types).

2. Call 'find' with:
   database   = "querysentinel"
   collection = "collection_baselines"
   filter     = {"collection_name": "<anomaly.collection_name>"}
   limit      = 1
   → Get the stored baseline schema from the querysentinel operational DB.

3. Compare live schema vs baseline schema:
   - added_fields:   fields present in live but not in baseline
   - removed_fields: fields present in baseline but not in live
   - type_changes:   fields present in both with different types

Return ONLY a raw JSON object (no markdown, no prose):
{
  "collection_name": "...",
  "drift_detected":  true|false,
  "added_fields":    ["field1", "field2"],
  "removed_fields":  ["field3"],
  "type_changes":    [{"field": "x", "was": "string", "now": "array"}],
  "live_field_count":  <number>,
  "base_field_count":  <number>,
  "drift_summary": "<one sentence>",
  "most_suspicious_field": "<field name most likely to be the root cause, or null>"
}
"""


def build_schema_agent(tools: list, callbacks: dict | None = None) -> LlmAgent:
    kwargs = {}
    if callbacks:
        kwargs.update(callbacks)
    return LlmAgent(
        name="SchemaDriftAgent",
        model=GEMINI_MODEL,
        instruction=INSTRUCTION,
        tools=tools,
        output_key="schema_result",
        **kwargs,
    )
