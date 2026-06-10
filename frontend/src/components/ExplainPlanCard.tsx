'use client';
/**
 * ExplainPlanCard.tsx — Before/After explain plan comparison.
 *
 * Shows the COLLSCAN → IXSCAN transformation proof after remediation.
 * This is the "closed-loop remediation" visual evidence:
 *   1. Before: COLLSCAN, 10,000+ docs examined
 *   2. After:  IXSCAN,   1 doc examined
 */
import clsx from 'clsx';
import { ArrowRight, XCircle, CheckCircle2 } from 'lucide-react';

interface ExplainStats {
  stage:          'COLLSCAN' | 'IXSCAN' | string;
  docsExamined:   number;
  docsReturned:   number;
  executionTimeMs: number;
  indexUsed?:     string;
}

interface ExplainPlanCardProps {
  before?: ExplainStats;
  after?:  ExplainStats;
  indexCreated?: string;
  status?: 'pending_approve' | 'index_building' | 'complete' | 'not_applicable';
}

export function ExplainPlanCard({
  before,
  after,
  indexCreated,
  status = 'pending_approve',
}: ExplainPlanCardProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          Explain Plan Comparison
        </h3>
        <StatusBadge status={status} />
      </div>

      {status === 'pending_approve' && (
        <div className="text-xs text-surface-4 bg-surface-2 rounded-lg p-3 text-center">
          Awaiting APPROVE to execute index creation and run post-index explain()
        </div>
      )}

      {status === 'index_building' && (
        <div className="text-xs text-yellow-400 bg-yellow-900/20 rounded-lg p-3 text-center animate-pulse">
          Index building… polling Atlas until ready
        </div>
      )}

      {(before || after) && (
        <div className="flex items-stretch gap-3">
          {/* Before */}
          <ExplainBox
            label="BEFORE (no index)"
            stats={before ?? { stage: '—', docsExamined: 0, docsReturned: 0, executionTimeMs: 0 }}
            bad={true}
          />

          {/* Arrow */}
          <div className="flex items-center text-mongo-light shrink-0">
            <ArrowRight size={16} />
          </div>

          {/* After */}
          <ExplainBox
            label="AFTER (index applied)"
            stats={after ?? { stage: '—', docsExamined: 0, docsReturned: 0, executionTimeMs: 0 }}
            bad={false}
          />
        </div>
      )}

      {indexCreated && (
        <div className="bg-surface-2 rounded-lg p-2.5">
          <p className="text-xs text-gray-400">Index created via MCP <code className="text-mongo-bright">create-index</code>:</p>
          <p className="mono text-xs text-white mt-1">{indexCreated}</p>
        </div>
      )}
    </div>
  );
}

function ExplainBox({ label, stats, bad }: { label: string; stats: ExplainStats; bad: boolean }) {
  const efficiency = stats.docsReturned && stats.docsExamined
    ? ((stats.docsReturned / stats.docsExamined) * 100).toFixed(1)
    : null;

  return (
    <div className={clsx(
      'flex-1 rounded-lg border p-3 space-y-2',
      bad
        ? 'bg-red-900/10 border-critical/30'
        : 'bg-green-900/10 border-success/30',
    )}>
      <div className="flex items-center gap-1.5">
        {bad
          ? <XCircle     size={12} className="text-critical" />
          : <CheckCircle2 size={12} className="text-success"  />
        }
        <span className="text-xs font-semibold text-gray-300">{label}</span>
      </div>

      <div className="space-y-1">
        <StatLine label="Stage"       value={stats.stage}           highlight={bad && stats.stage === 'COLLSCAN'} />
        <StatLine label="Docs examined" value={stats.docsExamined?.toLocaleString()} highlight={bad && stats.docsExamined > 1000} />
        <StatLine label="Docs returned" value={stats.docsReturned?.toLocaleString()} />
        <StatLine label="Time"         value={`${stats.executionTimeMs} ms`}        highlight={bad && stats.executionTimeMs > 100} />
        {stats.indexUsed && <StatLine label="Index" value={stats.indexUsed} />}
        {efficiency && <StatLine label="Efficiency" value={`${efficiency}%`} highlight={bad && parseFloat(efficiency) < 10} />}
      </div>
    </div>
  );
}

function StatLine({ label, value, highlight = false }: { label: string; value?: string | number; highlight?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-surface-4">{label}</span>
      <span className={clsx('text-xs font-medium mono', highlight ? 'text-critical' : 'text-white')}>
        {value ?? '—'}
      </span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; class: string }> = {
    pending_approve: { label: 'Awaiting APPROVE', class: 'badge-warning'  },
    index_building:  { label: 'Building Index…',  class: 'badge-warning'  },
    complete:        { label: 'Remediated',        class: 'badge-ok'       },
    not_applicable:  { label: 'N/A',               class: 'text-surface-4 text-xs' },
  };
  const m = map[status] ?? map.not_applicable;
  return <span className={m.class}>{m.label}</span>;
}
