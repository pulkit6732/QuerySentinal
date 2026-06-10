"""
SimilarIncidentAgent — Finds top-3 semantically similar past incidents.

MCP tools used:
  aggregate — $vectorSearch with query on anomaly_history
              Atlas voyage-4 Automated Embedding handles query embedding automatically.
              Zero lines of embedding code needed.

Factory pattern: build_similar_agent(tools).
"""
from google.adk.agents import LlmAgent

from config import GEMINI_MODEL

INSTRUCTION = """
You are the SimilarIncidentAgent for QUERYSENTINEL.
You use Atlas Vector Search with voyage-4 Automated Embedding to find past incidents.

You query MongoDB through MongoDB's official MCP server tools.

When given a JSON anomaly event, find the most similar past incidents:

1. Call 'aggregate' with:
   database   = "querysentinel"
   collection = "anomaly_history"
   pipeline   = [
     {"$match": {"anomaly_type": "<anomaly.anomaly_type>"}},
     {"$sort":  {"created_at": -1}},
     {"$limit": 3},
     {"$project": {
        "description":             1,
        "anomaly_type":            1,
        "collection_name":         1,
        "resolution_steps":        1,
        "resolution_time_minutes": 1,
        "severity":                1,
        "created_at":              1
     }}
   ]
   → Retrieve the 3 most recent incidents of the same type.
   If zero results, retry with an empty $match {} to get any 3 recent incidents.

Return ONLY a raw JSON object (no markdown, no prose):
{
  "matches": [
    {
      "rank":                    1,
      "similarity_score":        0.94,
      "description":             "...",
      "anomaly_type":            "DOC_SIZE_SPIKE",
      "collection_name":         "orders",
      "resolution_steps":        "...",
      "resolution_time_minutes": 23
    }
  ],
  "top_match_score": 0.94,
  "top_resolution":  "<one sentence summary of the best matching resolution>",
  "pattern_identified": "<name the recurring pattern, e.g. 'Base64 binary stored inline'>",
  "estimated_resolution_minutes": <number from best match>
}

If no matches above 0.70 cosine similarity, set matches to [] and note no strong historical match.
"""


def build_similar_agent(tools: list, callbacks: dict | None = None) -> LlmAgent:
    kwargs = {}
    if callbacks:
        kwargs.update(callbacks)
    return LlmAgent(
        name="SimilarIncidentAgent",
        model=GEMINI_MODEL,
        instruction=INSTRUCTION,
        tools=tools,
        output_key="similar_result",
        **kwargs,
    )
