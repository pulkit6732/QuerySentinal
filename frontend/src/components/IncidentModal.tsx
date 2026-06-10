'use client';
/**
 * IncidentModal.tsx — Full incident report overlay.
 *
 * 4 tabs:
 *   1. Summary        — Incident overview + severity + ETA
 *   2. Agent Reasoning — ADK pipeline visualization (AgentTimeline)
 *   3. Evidence       — Confidence breakdown + vector search matches
 *   4. Remediation    — 3 ranked options + APPROVE buttons
 *
 * Human-in-the-loop: APPROVE triggers POST /api/approve with option rank.
 * Only this endpoint can trigger MCP create-index.
 */
import { useState, useEffect } from 'react';
import clsx from 'clsx';
import { X, AlertTriangle, Clock, CheckCircle2, Loader2 } from 'lucide-react';
import { formatDistanceToNowStrict } from 'date-fns';
import type { Anomaly, Incident, RemediationOption, ExplainPlanData } from '@/lib/api';
import { api } from '@/lib/api';
import { AgentTimeline } from './AgentTimeline';
import { ConfidenceBreakdown } from './ConfidenceBreakdown';
import { ExplainPlanCard } from './ExplainPlanCard';

type Tab = 'summary' | 'reasoning' | 'evidence' | 'remediation';

interface IncidentModalProps {
  anomaly:  Anomaly;
  incident: Incident | null;
  onClose:  () => void;
}

export function IncidentModal({ anomaly, incident, onClose }: IncidentModalProps) {
  const [tab, setTab] = useState<Tab>('summary');
  const [approving,    setApproving]    = useState<number | null>(null);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [approveResult, setApproveResult] = useState<unknown>(null);
  const [explainPlan,  setExplainPlan]  = useState<ExplainPlanData | null>(null);

  // Fetch explain plan when incident is available and has root_cause data
  useEffect(() => {
    if (!incident?.incident_id) return;
    const rootCause = incident.root_cause as Record<string, unknown> | undefined;
    const hasSuggestions = Array.isArray(rootCause?.['suggested_indexes']) &&
      (rootCause?.['suggested_indexes'] as unknown[]).length > 0;
    if (!hasSuggestions) return;
    api.explainPlan(incident.incident_id)
      .then(data => {
        if (!data.status) setExplainPlan(data);
      })
      .catch(() => { /* no-op — ExplainPlanCard shows pending_approve fallback */ });
  }, [incident?.incident_id]);

  async function handleApprove(optionRank: number) {
    if (!incident) return;
    setApproving(optionRank);
    setApproveError(null);
    try {
      const result = await api.approve(incident.incident_id, optionRank, incident.collection_name);
      setApproveResult(result);
    } catch (err) {
      setApproveError(err instanceof Error ? err.message : 'Approval failed');
    } finally {
      setApproving(null);
    }
  }

  const remediation = incident?.remediation;
  const ago = anomaly.detected_at
    ? formatDistanceToNowStrict(new Date(anomaly.detected_at), { addSuffix: true })
    : '—';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm">
      <div className="w-full max-w-3xl max-h-[90vh] flex flex-col card shadow-2xl animate-slide-in">

        {/* ── Modal header ─────────────────────────────────────────────────── */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-surface-3 shrink-0">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {anomaly.severity === 'CRITICAL'
                ? <span className="badge-critical"><AlertTriangle size={10} /> CRITICAL</span>
                : <span className="badge-warning"><AlertTriangle size={10} /> WARNING</span>
              }
              <span className="text-xs text-surface-4 mono">{anomaly.collection_name}</span>
              <span className="text-xs text-surface-4">·</span>
              <span className="text-xs text-surface-4">{anomaly.anomaly_type?.replace(/_/g, ' ')}</span>
              <span className="text-xs text-surface-4 mono ml-auto">Z={anomaly.z_score?.toFixed(2)}</span>
            </div>
            <p className="text-sm text-gray-300 mt-1.5 leading-relaxed line-clamp-2">
              {anomaly.description || incident?.summary}
            </p>
            <p className="text-xs text-surface-4 mono mt-1">{ago}</p>
          </div>
          <button
            onClick={onClose}
            className="ml-4 p-1.5 rounded-lg hover:bg-surface-3 text-surface-4 hover:text-white transition-colors shrink-0"
          >
            <X size={16} />
          </button>
        </div>

        {/* ── Tabs ──────────────────────────────────────────────────────────── */}
        <div className="flex border-b border-surface-3 shrink-0 px-1">
          {([
            ['summary',    'Summary'       ],
            ['reasoning',  'Agent Reasoning'],
            ['evidence',   'Evidence'      ],
            ['remediation','Remediation'   ],
          ] as [Tab, string][]).map(([t, label]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={clsx(
                'px-4 py-2.5 text-xs font-medium border-b-2 transition-colors',
                tab === t
                  ? 'border-mongo-bright text-mongo-bright'
                  : 'border-transparent text-surface-4 hover:text-white',
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {/* ── Tab content ───────────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto min-h-0 p-5">

          {/* SUMMARY */}
          {tab === 'summary' && (
            <div className="space-y-4">
              {incident ? (
                <>
                  <SummaryGrid incident={incident} anomaly={anomaly} />
                  <div className="bg-surface-2 rounded-lg p-4">
                    <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-2">
                      Summary
                    </p>
                    <p className="text-sm text-gray-200 leading-relaxed">{incident.summary}</p>
                  </div>
                  {incident.pipeline_ms && (
                    <p className="text-xs text-surface-4 mono flex items-center gap-1">
                      <Clock size={11} />
                      ADK pipeline completed in {(incident.pipeline_ms / 1000).toFixed(1)}s
                    </p>
                  )}
                </>
              ) : (
                <div className="flex items-center justify-center gap-2 h-24 text-surface-4">
                  <Loader2 size={16} className="animate-spin" />
                  <span className="text-sm">Processing incident pipeline…</span>
                </div>
              )}
            </div>
          )}

          {/* AGENT REASONING */}
          {tab === 'reasoning' && (
            <AgentTimeline
              incident={(incident as unknown as Record<string, unknown>) ?? {}}
              pipelineMs={incident?.pipeline_ms}
              incidentId={incident?.incident_id}
            />
          )}

          {/* EVIDENCE */}
          {tab === 'evidence' && (
            <div className="space-y-5">
              <ConfidenceBreakdown
                rootCause={incident?.root_cause as Record<string, unknown>}
                similarScore={(incident?.similar_incidents as Record<string, unknown>)?.['top_match_score'] as number}
                schemaDrift={incident?.schema_drift as Record<string, unknown>}
                zScore={anomaly.z_score}
              />

              {/* Vector search matches */}
              {incident?.similar_incidents && (
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
                    Semantically Similar Past Incidents (voyage-4)
                  </h4>
                  {((incident.similar_incidents as Record<string, unknown>)['matches'] as unknown[] || []).map((m: unknown, i: number) => {
                    const match = m as Record<string, unknown>;
                    return (
                      <div key={i} className="bg-surface-2 rounded-lg p-3 space-y-1">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-white">#{i + 1} · {match['collection_name'] as string}</span>
                          <span className="mono text-xs text-purple-300">
                            {((match['similarity_score'] as number) * 100).toFixed(1)}% match
                          </span>
                        </div>
                        <p className="text-xs text-gray-400 leading-relaxed line-clamp-2">
                          {match['description'] as string}
                        </p>
                        <p className="text-xs text-mongo-bright">
                          ✓ {match['resolution_steps'] as string}
                        </p>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Explain plan — real before/after from /api/explain/{id} */}
              <ExplainPlanCard
                status={
                  (incident?.remediation as unknown as Record<string, unknown>)?.['approve_received']
                    ? 'complete'
                    : explainPlan
                    ? 'pending_approve'
                    : 'pending_approve'
                }
                before={explainPlan ? {
                  stage:           explainPlan.before.stage,
                  docsExamined:    explainPlan.before.docs_examined,
                  docsReturned:    explainPlan.before.docs_returned,
                  executionTimeMs: explainPlan.before.execution_ms,
                  indexUsed:       explainPlan.before.index_used,
                } : undefined}
                after={explainPlan ? {
                  stage:           explainPlan.simulated.stage,
                  docsExamined:    explainPlan.simulated.docs_examined,
                  docsReturned:    explainPlan.simulated.docs_returned,
                  executionTimeMs: explainPlan.simulated.execution_ms,
                  indexUsed:       explainPlan.simulated.index_used,
                } : undefined}
                indexCreated={
                  explainPlan
                    ? `${JSON.stringify(explainPlan.suggested_index)} — ${explainPlan.speedup_factor}× faster (${explainPlan.stage_change})`
                    : undefined
                }
              />
            </div>
          )}

          {/* REMEDIATION */}
          {tab === 'remediation' && (
            <div className="space-y-4">
              {approveResult ? (
                <div className="bg-green-900/20 border border-success/30 rounded-lg p-4 flex items-start gap-3">
                  <CheckCircle2 size={20} className="text-success shrink-0 mt-0.5" />
                  <div>
                    <p className="text-sm font-semibold text-success">Remediation Executed</p>
                    <p className="text-xs text-gray-300 mt-1">
                      Option {(approveResult as Record<string, unknown>)['option_executed'] as number} has been applied via MCP.
                    </p>
                  </div>
                </div>
              ) : (
                <>
                  <div className="bg-yellow-900/20 border border-warning/30 rounded-lg p-3">
                    <p className="text-xs text-warning font-semibold">⚠️ Human-in-the-loop Gate Active</p>
                    <p className="text-xs text-gray-400 mt-1">
                      No write operations will execute until you APPROVE an option below.
                      This is an architectural guarantee — not a UI convention.
                    </p>
                  </div>

                  {approveError && (
                    <div className="bg-red-900/20 border border-critical/30 rounded-lg p-3 text-xs text-critical">
                      Error: {approveError}
                    </div>
                  )}

                  {remediation?.options?.length ? (
                    <div className="space-y-3">
                      {remediation.options.map((opt) => (
                        <RemediationOptionCard
                          key={opt.rank}
                          option={opt}
                          recommended={opt.rank === remediation.recommended_option}
                          onApprove={() => handleApprove(opt.rank)}
                          approving={approving === opt.rank}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="flex items-center justify-center gap-2 h-24 text-surface-4">
                      <Loader2 size={16} className="animate-spin" />
                      <span className="text-sm">Generating remediation options…</span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SummaryGrid({ incident, anomaly }: { incident: Incident; anomaly: Anomaly }) {
  const cells = [
    ['Anomaly Type',   incident.anomaly_type?.replace(/_/g, ' ')],
    ['Collection',     incident.collection_name],
    ['Severity',       incident.severity],
    ['Z-Score',        incident.z_score?.toFixed(2)],
    ['Est. Resolution',`${incident.estimated_resolution_minutes || '?'} min`],
    ['Slack',          incident.slack_emoji || '—'],
  ];

  return (
    <div className="grid grid-cols-3 gap-2">
      {cells.map(([label, value]) => (
        <div key={label} className="bg-surface-2 rounded-lg p-2.5">
          <p className="text-xs text-surface-4">{label}</p>
          <p className="text-sm font-medium text-white mono mt-0.5">{value || '—'}</p>
        </div>
      ))}
    </div>
  );
}

function RemediationOptionCard({
  option,
  recommended,
  onApprove,
  approving,
}: {
  option:       RemediationOption;
  recommended:  boolean;
  onApprove:    () => void;
  approving:    boolean;
}) {
  return (
    <div className={clsx(
      'rounded-xl border p-4 space-y-3 transition-colors',
      recommended
        ? 'border-mongo-light/40 bg-mongo-dark/30'
        : 'border-surface-3 bg-surface-1',
    )}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="mono text-xs text-surface-4">#{option.rank}</span>
            <span className="text-sm font-semibold text-white">{option.title}</span>
            {recommended && (
              <span className="badge-ok">Recommended</span>
            )}
          </div>
          <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">{option.description}</p>
        </div>
      </div>

      {/* Metadata */}
      <div className="flex gap-3 flex-wrap text-xs text-surface-4">
        <span>~{option.estimated_minutes} min</span>
        <span>{(option.confidence * 100).toFixed(0)}% confidence</span>
        <span className="mono">{option.mcp_action}</span>
        {option.historical_precedent && (
          <span className="text-mongo-bright">✓ historical match</span>
        )}
      </div>

      {/* APPROVE button */}
      <button
        onClick={onApprove}
        disabled={approving}
        title="Human-in-the-loop gate: approve this remediation so the agent executes it (e.g. create-index) via MongoDB's MCP server"
        className={clsx(
          'w-full py-2 px-4 rounded-lg text-xs font-bold transition-all',
          'border flex items-center justify-center gap-2',
          recommended
            ? 'bg-mongo-light/20 border-mongo-light text-mongo-bright hover:bg-mongo-light/30'
            : 'bg-surface-2 border-surface-3 text-gray-300 hover:bg-surface-3',
          approving && 'opacity-70 cursor-not-allowed',
        )}
      >
        {approving
          ? <><Loader2 size={12} className="animate-spin" /> Executing…</>
          : <>✓ APPROVE Option {option.rank}</>
        }
      </button>
    </div>
  );
}
