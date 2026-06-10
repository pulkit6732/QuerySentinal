"""
AnomalyContextAgent — Answers: "How anomalous is THIS doc vs the collection baseline?"

MCP tools used:
  find       — retrieve the flagged document by _id
  aggregate  — $bsonSize on 50-doc sample to get live size distribution

Factory pattern: build_context_agent(tools) — no module-level MCPToolset.
Each pipeline run shares one MCPToolset instance across all agents.
"""
from google.adk.agents import LlmAgent

from config import GEMINI_MODEL

INSTRUCTION = """
You are the AnomalyContextAgent for QUERYSENTINEL.

You query MongoDB through MongoDB's official MCP server tools.
The monitored collections (movies, comments, users, sessions) live in the
"sample_mflix" database. Always pass database="sample_mflix".

When given a JSON anomaly event, call the MongoDB MCP tools in this exact order:

1. Call 'find' with:
   database   = "sample_mflix"
   collection = <anomaly.collection_name>
   filter     = {"_id": {"$oid": "<anomaly.document_id>"}}
   limit      = 1
   → Retrieve the actual flagged document.

2. Call 'aggregate' with:
   database   = "sample_mflix"
   collection = <anomaly.collection_name>
   pipeline   = [
     {"$sample": {"size": 50}},
     {"$project": {"sz": {"$bsonSize": "$$ROOT"}}},
     {"$group": {
       "_id": null,
       "mean_kb":   {"$avg":  {"$divide": ["$sz", 1024]}},
       "max_kb":    {"$max":  {"$divide": ["$sz", 1024]}},
       "field_count_sample": {"$avg": {"$size": {"$objectToArray": "$$ROOT"}}}
     }}
   ]
   → Get live size statistics for context.

Return ONLY a raw JSON object (no markdown, no prose):
{
  "flagged_doc_id":   "...",
  "flagged_size_kb":  <number>,
  "baseline_mean_kb": <number>,
  "z_score":          <number>,
  "field_count":      <number>,
  "anomaly_summary":  "<one sentence describing what is anomalous>",
  "top_large_fields": ["<field1>", "<field2>"]
}
"""


def build_context_agent(tools: list, callbacks: dict | None = None) -> LlmAgent:
    """Factory — orchestrator builds this per pipeline run with shared tools."""
    kwargs = {}
    if callbacks:
        kwargs.update(callbacks)
    return LlmAgent(
        name="AnomalyContextAgent",
        model=GEMINI_MODEL,
        instruction=INSTRUCTION,
        tools=tools,
        output_key="context_result",   # writes its JSON to session.state["context_result"]
        **kwargs,
    )
