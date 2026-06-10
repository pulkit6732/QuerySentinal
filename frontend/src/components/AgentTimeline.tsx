'use client';
/**
 * AgentTimeline.tsx — Real ADK multi-agent execution timeline.
 *
 * When incidentId is provided it fetches /api/agent-calls/{id} and renders:
 *  - A Gantt-style parallel bar visualization: 4 agents running concurrently
 *    (ADK ParallelAgent), then RemediationAgent after.
 *  - Real MCP tool call names + count per agent from _StepTracker output.
 *  - Parallelism speedup metric: sequential_sum / actual_wall_clock.
 *
 * Falls back to static synthetic view when trace is unavailable.
 */
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import clsx from 'clsx';
import {
  Brain, Search, GitBranch, Shield, Wrench,
  CheckCircle2, Clock, Loader2, Zap,
} from 'lucide-react';
import { api } from '@/lib/api';
import type { AgentCallTrace, AgentStepTrace } from '@/lib/api';

// ── Agent metadata ────────────────────────────────────────────────────────────

const AGENT_META: Record<string, { role: string; color: string; icon: ReactNode }> = {
  AnomalyContextAgent:   { role: 'Quantifies anomaly severity',            color: 'text-white',   icon: <Search size={12} /> },
  SchemaDriftAgent:      { role: 'Detects schema changes',                  color: 'text-yellow-400', icon: <GitBranch size={12} /> },
  SimilarIncidentAgent:  { role: 'Vector Search — past resolutions',        color: 'text-purple-400', icon: <Brain size={12} /> },
  RootCauseAgent:        { role: 'Queries Atlas Performance Advisor',       color: 'text-green-400',  icon: <Shield size={12} /> },
  RemediationAgent:      { role: 'Ranks options — APPROVE gate',            color: 'text-orange-400', icon: <Wrench size={12} /> },
  ParallelInvestigation: { role: 'ADK ParallelAgent wrapper',               color: 'text-surface-4',  icon: <Zap size={12} /> },
  IncidentPipeline:      { role: 'ADK SequentialAgent root',                color: 'text-surface-4',  icon: <Zap size={12} /> },
};

const PARALLEL_AGENTS = new Set([
  'AnomalyContextAgent',
  'SchemaDriftAgent',
  'SimilarIncidentAgent',
  'RootCauseAgent',
]);

// ── Gantt trace view (real data) ──────────────────────────────────────────────

function GanttView({ trace }: { trace: AgentCallTrace }) {
  const totalMs = trace.total_ms || 1;

  // Group steps by agent name, keep last occurrence (final state)
  const byAgent = new Map<string, AgentStepTrace>();
  for (const step of trace.steps) {
    if (AGENT_META[step.agent]) {
      byAgent.set(step.agent, step);
    }
  }

  const parallelSteps = ['AnomalyContextAgent', 'SchemaDriftAgent', 'SimilarIncidentAgent', 'RootCauseAgent']
    .map(n => byAgent.get(n))
    .filter(Boolean) as AgentStepTrace[];

  const remediationStep = byAgent.get('RemediationAgent');

  // Parallelism speedup: sequential sum of parallel durations / wall-clock parallel phase
  const parallelSum = parallelSteps.reduce((a, s) => a + (s.duration_ms ?? 0), 0);
  const parallelWall = parallelSteps.length
    ? Math.max(...parallelSteps.map(s => s.end_ms ?? s.start_ms)) -
      Math.min(...parallelSteps.map(s => s.start_ms))
    : 0;
  const speedup = parallelWall > 0 ? (parallelSum / parallelWall).toFixed(1) : null;

  // All MCP tool calls across all agents
  const allToolCalls = trace.steps.flatMap(s => s.tool_calls ?? []);
  const toolCounts = allToolCalls.reduce<Record<string, number>>((acc, tc) => {
    acc[tc.name] = (acc[tc.name] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          ADK Agent Reasoning Pipeline
        </h3>
        <span className="flex items-center gap-1 text-xs text-surface-4 mono">
          <Clock size={11} />
          {(totalMs / 1000).toFixed(1)}s total
        </span>
      </div>

      {/* Parallelism callout */}
      {speedup && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-mongo-dark/40 border border-mongo-mid/30">
          <Zap size={13} className="text-mongo-bright shrink-0" />
          <p className="text-xs text-mongo-bright">
            <span className="font-bold">{speedup}× faster</span>
            <span className="text-gray-400"> — 4 agents ran concurrently via </span>
            <span className="mono">ParallelAgent</span>
            <span className="text-gray-400"> ({(parallelWall / 1000).toFixed(1)}s wall vs {(parallelSum / 1000).toFixed(1)}s sequential)</span>
          </p>
        </div>
      )}

      {/* Parallel phase */}
      <div className="space-y-1.5">
        <p className="text-xs text-surface-4 mono uppercase tracking-widest">
          ┌ Parallel Investigation
        </p>
        {parallelSteps.map(step => (
          <GanttRow key={step.agent} step={step} totalMs={totalMs} />
        ))}
        <p className="text-xs text-surface-4 mono">└</p>
      </div>

      {/* Sequential: RemediationAgent */}
      {remediationStep && (
        <div className="space-y-1.5">
          <p className="text-xs text-surface-4 mono uppercase tracking-widest">
            Remediation (sequential, after parallel)
          </p>
          <GanttRow step={remediationStep} totalMs={totalMs} />
        </div>
      )}

      {/* MCP tool call summary */}
      {Object.keys(toolCounts).length > 0 && (
        <div>
          <p className="text-xs text-surface-4 mb-1.5">MCP tool calls</p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(toolCounts).map(([name, count]) => (
              <span
                key={name}
                className="mono text-xs bg-surface-2 border border-surface-3 rounded px-2 py-0.5 text-gray-300"
              >
                {name}
                {count > 1 && <span className="text-surface-4 ml-1">×{count}</span>}
              </span>
            ))}
          </div>
        </div>
      )}

      <MemoryCallout />
    </div>
  );
}

function GanttRow({ step, totalMs }: { step: AgentStepTrace; totalMs: number }) {
  const meta     = AGENT_META[step.agent] ?? { role: '', color: 'text-gray-400', icon: null };
  const startPct = ((step.start_ms / totalMs) * 100).toFixed(2);
  const widthPct = (((step.duration_ms ?? 0) / totalMs) * 100).toFixed(2);
  const toolNames = (step.tool_calls ?? []).map(t => t.name);
  const uniqueTools = [...new Set(toolNames)];

  return (
    <div className="flex items-start gap-3">
      {/* Label */}
      <div className="w-44 shrink-0 pt-0.5">
        <div className="flex items-center gap-1.5">
          <span className={clsx('shrink-0', meta.color)}>{meta.icon}</span>
          <span className={clsx('text-xs font-medium truncate', meta.color)}>
            {step.agent}
          </span>
        </div>
        {uniqueTools.length > 0 && (
          <p className="text-xs text-surface-4 mono mt-0.5 truncate">
            {uniqueTools.join(' · ')}
          </p>
        )}
      </div>

      {/* Gantt bar */}
      <div className="flex-1 flex items-center gap-2">
        <div className="flex-1 relative h-5 bg-surface-2 rounded overflow-hidden">
          <div
            className={clsx(
              'absolute top-0 h-full rounded transition-all duration-500',
              step.agent === 'AnomalyContextAgent'  && 'bg-white/60',
              step.agent === 'SchemaDriftAgent'     && 'bg-yellow-500/70',
              step.agent === 'SimilarIncidentAgent' && 'bg-purple-500/70',
              step.agent === 'RootCauseAgent'       && 'bg-green-500/70',
              step.agent === 'RemediationAgent'     && 'bg-orange-500/70',
            )}
            style={{ left: `${startPct}%`, width: `${widthPct}%`, minWidth: 4 }}
          />
        </div>
        <span className="text-xs mono text-surface-4 shrink-0 w-14 text-right">
          {step.duration_ms != null ? `${(step.duration_ms / 1000).toFixed(2)}s` : '—'}
        </span>
        <CheckCircle2 size={13} className="text-mongo-light shrink-0" />
      </div>
    </div>
  );
}

// ── Synthetic fallback view (no trace available yet) ──────────────────────────

function SyntheticView({
  incident,
  pipelineMs,
}: {
  incident: Record<string, unknown>;
  pipelineMs?: number;
}) {
  const ctx    = incident['context']           as Record<string, unknown> | undefined;
  const schema = incident['schema_drift']      as Record<string, unknown> | undefined;
  const sim    = incident['similar_incidents'] as Record<string, unknown> | undefined;
  const rc     = incident['root_cause']        as Record<string, unknown> | undefined;
  const remed  = incident['remediation']       as Record<string, unknown> | undefined;

  const steps = [
    {
      agent:   'AnomalyContextAgent',
      role:    'Quantifies anomaly severity',
      done:    !!ctx,
      tools:   ['find', 'aggregate ($bsonSize)'],
      summary: ctx
        ? `Z=${(ctx['z_score'] as number)?.toFixed(2)} · ${((ctx['flagged_size_kb'] as number) || 0).toFixed(0)} KB · baseline ${((ctx['baseline_mean_kb'] as number) || 0).toFixed(1)} KB mean`
        : 'Waiting…',
    },
    {
      agent:   'SchemaDriftAgent',
      role:    'Detects schema changes',
      done:    !!schema,
      tools:   ['collection-schema', 'find'],
      summary: schema
        ? `+${(schema['added_fields'] as unknown[])?.length || 0} fields · -${(schema['removed_fields'] as unknown[])?.length || 0} removed · ${(schema['type_changes'] as unknown[])?.length || 0} type changes`
        : 'Waiting…',
    },
    {
      agent:   'SimilarIncidentAgent',
      role:    'Vector Search — past resolutions',
      done:    !!sim,
      tools:   ['aggregate ($vectorSearch)'],
      summary: sim
        ? `Top match: ${((sim['top_match_score'] as number) || 0).toFixed(2)} similarity · Est. ${(sim['estimated_resolution_minutes'] as number) || '?'} min`
        : 'Waiting…',
    },
    {
      agent:   'RootCauseAgent',
      role:    'Queries Atlas Performance Advisor',
      done:    !!rc,
      tools:   ['atlas-get-performance-advisor'],
      summary: rc
        ? `${rc['root_cause_type'] as string} · confidence ${((rc['confidence'] as number) || 0).toFixed(2)}`
        : 'Waiting…',
    },
    {
      agent:   'RemediationAgent',
      role:    'Ranks options — APPROVE gate',
      done:    !!remed,
      tools:   remed?.['approve_received'] ? ['create-index (APPROVED)'] : ['(read-only)'],
      summary: remed
        ? `${((remed['options'] as unknown[]) || []).length} options ready`
        : 'Waiting…',
    },
  ];

  return (
    <div className="space-y-0">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          ADK Agent Reasoning Pipeline
        </h3>
        {pipelineMs && (
          <span className="flex items-center gap-1 text-xs text-surface-4 mono">
            <Clock size={11} />
            {(pipelineMs / 1000).toFixed(1)}s total
          </span>
        )}
      </div>

      {steps.map((step, i) => {
        const meta = AGENT_META[step.agent];
        return (
          <div key={step.agent} className="flex gap-3">
            <div className="flex flex-col items-center" style={{ width: 24 }}>
              <div className={clsx(
                'rounded-full border-2 flex items-center justify-center shrink-0 w-6 h-6',
                step.done ? 'border-mongo-light bg-mongo-dark' : 'border-surface-3 bg-surface-2',
              )}>
                {step.done
                  ? <CheckCircle2 size={12} className="text-mongo-light" />
                  : <span className={clsx('text-xs', meta.color)}>{meta.icon}</span>
                }
              </div>
              {i < steps.length - 1 && (
                <div className={clsx('w-px flex-1 my-1', step.done ? 'bg-mongo-mid' : 'bg-surface-3')}
                  style={{ minHeight: 16 }} />
              )}
            </div>

            <div className={clsx('flex-1 pb-4', i === steps.length - 1 && 'pb-0')}>
              <div className="flex items-center gap-2 flex-wrap">
                <span className={clsx('text-xs font-semibold', meta.color)}>{step.agent}</span>
                <span className="text-xs text-surface-4">·</span>
                <span className="text-xs text-surface-4">{step.role}</span>
                <span className="mono text-xs text-surface-4 ml-auto">gemini-3.5-flash</span>
              </div>
              <div className="flex flex-wrap gap-1 mt-1">
                {step.tools.map(tool => (
                  <span key={tool}
                    className="mono text-xs bg-surface-2 border border-surface-3 rounded px-1.5 py-0.5 text-gray-400">
                    {tool}
                  </span>
                ))}
              </div>
              {step.done && (
                <p className="text-xs text-gray-300 mt-1.5 leading-relaxed">{step.summary}</p>
              )}
            </div>
          </div>
        );
      })}

      <MemoryCallout />
    </div>
  );
}

// ── Shared MongoDB memory callout ─────────────────────────────────────────────

function MemoryCallout() {
  return (
    <div className="mt-4 p-3 rounded-lg bg-mongo-dark/30 border border-mongo-mid/30">
      <p className="text-xs text-mongo-bright font-semibold mb-1">
        MongoDB as the Agentic Memory Layer
      </p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        {[
          ['Episodic Memory',  'anomaly_history (voyage-4 vectors)'],
          ['Working Memory',   'stream_state (resume tokens)'],
          ['Long-term Memory', 'collection_baselines'],
          ['Output Store',     'incident_reports'],
        ].map(([type, coll]) => (
          <div key={type} className="flex items-start gap-1">
            <span className="text-xs text-mongo-light shrink-0 mt-0.5">▸</span>
            <div>
              <span className="text-xs font-medium text-white">{type}:</span>
              <span className="text-xs text-gray-400 mono ml-1">{coll}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Public component ──────────────────────────────────────────────────────────

interface AgentTimelineProps {
  incident:    Record<string, unknown>;
  pipelineMs?: number;
  incidentId?: string;
}

export function AgentTimeline({ incident, pipelineMs, incidentId }: AgentTimelineProps) {
  const [trace,   setTrace]   = useState<AgentCallTrace | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!incidentId) return;
    setLoading(true);
    api.agentCalls(incidentId)
      .then(setTrace)
      .catch(() => setTrace(null))
      .finally(() => setLoading(false));
  }, [incidentId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 h-20 text-surface-4">
        <Loader2 size={14} className="animate-spin" />
        <span className="text-xs">Fetching agent execution trace…</span>
      </div>
    );
  }

  if (trace && trace.steps?.length > 0) {
    return <GanttView trace={trace} />;
  }

  return <SyntheticView incident={incident} pipelineMs={pipelineMs} />;
}
