/**
 * api.ts — Typed API client for QUERYSENTINEL backend.
 *
 * All types match exactly what the backend returns — verified against
 * detect.py, main.py, and the agent pipeline outputs.
 */

const BASE = (typeof window !== 'undefined' ? '' : (process.env.NEXT_PUBLIC_API_URL || ''));

// ── Types — match backend field names EXACTLY ─────────────────────────────────

export type Severity = 'CRITICAL' | 'WARNING';

export type AnomalyType =
  | 'DOC_SIZE_SPIKE'
  | 'SCHEMA_DRIFT'
  | 'WRITE_VOLUME_SPIKE'
  | 'SEMANTIC_VELOCITY_SPIKE';

export interface Anomaly {
  _id:             string;
  collection_name: string;
  anomaly_type:    AnomalyType;
  severity:        Severity;
  z_score:         number;
  detected_at:     string;
  description:     string;
  doc_size_bytes?: number;
  doc_size_kb?:    number;
  baseline_mean_kb?: number;
  confidence?:     number;
}

export interface RemediationOption {
  rank:                 number;
  title:                string;
  description:          string;
  estimated_minutes:    number;
  confidence:           number;
  historical_precedent: string | null;
  mcp_action:           string;
  index_keys?:          Record<string, unknown>;
}

export interface Remediation {
  approve_required?:  boolean;
  approve_received?:  boolean;
  options:            RemediationOption[];
  recommended_option: number;
  warning?:           string;
  executed_option?:   number;
  execution_status?:  string;
  action_taken?:      string;
}

export interface Incident {
  _id:                           string;
  incident_id:                   string;
  collection_name:               string;
  anomaly_type:                  AnomalyType;
  severity:                      Severity;
  z_score:                       number;
  detected_at:                   string;
  created_at:                    string;
  pipeline_ms:                   number;
  summary:                       string;
  slack_emoji:                   string;
  estimated_resolution_minutes:  number;
  anomaly_event?:                Record<string, unknown>;
  context?:                      Record<string, unknown>;
  schema_drift?:                 Record<string, unknown>;
  similar_incidents?:            Record<string, unknown>;
  root_cause?:                   Record<string, unknown>;
  remediation?:                  Remediation;
}

// ── detect.py compute_incident_runway() return shape ─────────────────────────
export interface RunwayResult {
  collection_name:        string;
  current_rate_per_min:   number;
  projected_critical_at:  string | null;
  minutes_to_critical:    number | null;
  trend:                  'increasing' | 'stable' | 'decreasing';
  confidence:             number;
  status:                 string;
}

// ── detect.py compute_semantic_velocity() return shape ───────────────────────
// Field names match the backend EXACTLY (centroid_distance, baseline_distance, etc.)
export interface SemanticVelocityResult {
  collection_name:    string;
  centroid_distance:  number;   // current hour cosine distance
  baseline_distance:  number;   // historical average cosine distance
  velocity_ratio:     number;   // centroid_distance / baseline_distance
  velocity_zscore?:   number;
  status:             'normal' | 'elevated' | 'spike' | 'insufficient_data';
  hourly_distances:   number[]; // sparkline data points
  window_hours:       number;
}

// ── explain.py get_explain_before_after() return shape ───────────────────────
export interface ExplainStats {
  stage:          string;
  docs_examined:  number;
  docs_returned:  number;
  execution_ms:   number;
  index_used?:    string;
  error?:         string;
  note?:          string;
}

export interface ExplainPlanData {
  incident_id:               string;
  collection:                string;
  before:                    ExplainStats;
  simulated:                 ExplainStats;
  speedup_factor:            number;
  stage_change:              string;
  execution_ms_reduction:    string;
  docs_examined_reduction:   string;
  summary:                   string;
  suggested_index:           Record<string, unknown>;
  status?:                   string;
  message?:                  string;
}

// ── orchestrator.py _StepTracker → app_db.agent_calls shape ─────────────────
export interface AgentStepTrace {
  agent:       string;
  start_ms:    number;
  end_ms:      number | null;
  duration_ms: number | null;
  tool_calls:  { name: string; at_ms: number }[];
  text_chunks: number;
}

export interface AgentCallTrace {
  incident_id:     string;
  collection_name: string;
  severity:        string;
  total_ms:        number;
  steps:           AgentStepTrace[];
  created_at:      string;
}

// ── detect.py build_database_health_card() return shape ──────────────────────
export interface CollectionHealthCard {
  collection:    string;
  health_score:  number;
  doc_count:     number;
  mean_size_kb:  number;   // note: mean_size_kb (not mean_kb)
  p50_size_kb:   number;
  p95_size_kb:   number;
  p99_size_kb:   number;   // note: p99_size_kb (not p99_kb)
  max_size_kb:   number;
  index_count:   number;
  status:        string;
  issues:        string[];
}

export interface HealthCard {
  cards:            CollectionHealthCard[];
  built_at:         string;
  db_name:          string;
  collection_count: number;
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
    cache:   'no-store',
  });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// ── Public API ────────────────────────────────────────────────────────────────

export const api = {
  anomalies: (limit = 50) =>
    get<{ anomalies: Anomaly[]; count: number }>(`/api/anomalies?limit=${limit}`),

  incidents: (limit = 20) =>
    get<{ incidents: Incident[]; count: number }>(`/api/incidents?limit=${limit}`),

  runway: () =>
    get<{ runway: Record<string, RunwayResult>; timestamp: string }>('/api/runway'),

  semanticVelocity: () =>
    get<{ velocity: Record<string, SemanticVelocityResult>; timestamp: string }>(
      '/api/semantic-velocity'
    ),

  healthCard: () =>
    get<HealthCard>('/api/health-card'),

  health: () =>
    get<{ status: string; components: Record<string, unknown>; timestamp: string }>('/health'),

  injectTest: () =>
    post<{
      status:          string;
      document_id:     string;
      approx_size_kb:  number;
      message:         string;
    }>('/api/inject-test', {}),

  approve: (incidentId: string, optionRank: number, collectionName: string) =>
    post<{
      status:          string;
      incident_id:     string;
      option_executed: number;
      result:          unknown;
    }>('/api/approve', {
      incident_id:     incidentId,
      option_rank:     optionRank,
      collection_name: collectionName,
    }),

  refreshBaseline: (collectionName: string) =>
    post<{
      status:     string;
      collection: string;
      mean_kb:    number;
      stddev_kb:  number;
      sample:     number;
    }>('/api/baseline/refresh', { collection_name: collectionName }),

  agentCalls: (incidentId: string) =>
    get<AgentCallTrace>(`/api/agent-calls/${incidentId}`),

  explainPlan: (incidentId: string) =>
    get<ExplainPlanData>(`/api/explain/${incidentId}`),
};
