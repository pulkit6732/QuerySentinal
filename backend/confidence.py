"""
confidence.py — Weighted confidence breakdown for QUERYSENTINEL.

Each agent output carries a machine-readable confidence score AND a
human-readable breakdown of the four evidence sources that contributed to it.

Displayed in the UI as a Confidence Breakdown Card with a stacked bar chart.
"""


def compute_confidence_breakdown(
    pa_result:    dict,    # output of atlas-get-performance-advisor
    vector_score: float,   # top $vectorSearch similarity (0–1)
    schema_drift: dict,    # output of detect_schema_drift()
    z_score:      float,   # anomaly Z-score
) -> dict:
    """
    Returns total_confidence (0–1) and evidence_breakdown list.

    Weights:
      Performance Advisor  35%   — diagnostic authority from Atlas itself
      Vector Search        30%   — semantic match to solved past incidents
      Schema Drift         20%   — structural evidence of root cause
      Z-score              15%   — statistical severity confirmation
    """
    evidence = []
    total    = 0.0

    # ── 1. Performance Advisor (weight 0.35) ──────────────────────────────────
    n_idx    = len(pa_result.get("suggestedIndexes",    []))
    n_schema = len(pa_result.get("schemaSuggestions",   []))
    n_slow   = len(pa_result.get("slowQueryLogs",       []))
    pa_conf  = min(1.0, n_idx * 0.30 + n_schema * 0.15 + min(n_slow, 5) * 0.05)
    contrib  = pa_conf * 0.35
    total   += contrib
    evidence.append({
        "source":      "Performance Advisor MCP (atlas-get-performance-advisor)",
        "raw_value":   f"{n_idx} index suggestions · {n_schema} schema warnings · {n_slow} slow queries",
        "confidence":  round(pa_conf, 3),
        "weight":      0.35,
        "contribution":round(contrib, 3),
        "color":       "#0073E6",   # blue — used in UI stacked bar
    })

    # ── 2. Vector Search similarity (weight 0.30) ─────────────────────────────
    vs_contrib = round(vector_score * 0.30, 3)
    total     += vs_contrib
    evidence.append({
        "source":      "$vectorSearch (voyage-4 Automated Embedding)",
        "raw_value":   f"Top match cosine similarity: {vector_score:.4f}",
        "confidence":  round(vector_score, 4),
        "weight":      0.30,
        "contribution":vs_contrib,
        "color":       "#00A35C",   # green
    })

    # ── 3. Schema Drift (weight 0.20) ─────────────────────────────────────────
    drift_detected = schema_drift.get("drift_detected", False)
    added  = len(schema_drift.get("added_fields",   []))
    removed= len(schema_drift.get("removed_fields", []))
    sd_conf = 0.9 if drift_detected else 0.15
    if drift_detected and (added + removed) > 2:
        sd_conf = min(1.0, sd_conf + 0.05 * (added + removed))
    sd_contrib = round(sd_conf * 0.20, 3)
    total     += sd_contrib
    evidence.append({
        "source":      "SchemaDriftAgent (collection-schema MCP diff)",
        "raw_value":   (f"Drift detected: {drift_detected} — "
                        f"+{added} added · -{removed} removed fields"),
        "confidence":  round(sd_conf, 3),
        "weight":      0.20,
        "contribution":sd_contrib,
        "color":       "#BF360C",   # orange
    })

    # ── 4. Z-score (weight 0.15) ──────────────────────────────────────────────
    z_conf    = min(1.0, abs(z_score) / 10.0)
    z_contrib = round(z_conf * 0.15, 3)
    total    += z_contrib
    evidence.append({
        "source":      f"Z-score statistical detector (Z = {z_score:.2f})",
        "raw_value":   f"|Z| = {abs(z_score):.2f} (CRITICAL threshold: 3.5)",
        "confidence":  round(z_conf, 3),
        "weight":      0.15,
        "contribution":z_contrib,
        "color":       "#B7770D",   # gold
    })

    total = round(total, 3)
    top_two = sorted(evidence, key=lambda x: x["contribution"], reverse=True)[:2]

    return {
        "total_confidence": total,
        "confidence_pct":   round(total * 100, 1),
        "evidence_breakdown": evidence,
        "interpretation": (
            f"System is {round(total*100,1)}% confident in this root cause. "
            f"Primary drivers: {top_two[0]['source'].split('(')[0].strip()} "
            f"({top_two[0]['contribution']} pts) + "
            f"{top_two[1]['source'].split('(')[0].strip()} "
            f"({top_two[1]['contribution']} pts)."
        ),
        # For UI stacked bar — values are fractions of the bar width
        "bar_segments": [
            {"label": "PA",     "value": evidence[0]["contribution"], "color": evidence[0]["color"]},
            {"label": "VS",     "value": evidence[1]["contribution"], "color": evidence[1]["color"]},
            {"label": "Schema", "value": evidence[2]["contribution"], "color": evidence[2]["color"]},
            {"label": "Z",      "value": evidence[3]["contribution"], "color": evidence[3]["color"]},
        ],
    }
