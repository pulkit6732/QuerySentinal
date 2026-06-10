'use client';
/**
 * DatabaseHealthCard.tsx — Atlas database health survey.
 * Field names match detect.py build_database_health_card() exactly:
 *   health_score, p99_size_kb (not p99_kb), mean_size_kb, issues[]
 */
import clsx from 'clsx';
import { Heart, AlertCircle } from 'lucide-react';
import type { HealthCard, CollectionHealthCard } from '@/lib/api';

function scoreColor(score: number): string {
  if (score >= 8) return 'text-success';
  if (score >= 5) return 'text-warning';
  return 'text-critical';
}

function scoreBorderBg(score: number): string {
  if (score >= 8) return 'bg-green-900/20 border-success/20';
  if (score >= 5) return 'bg-yellow-900/20 border-warning/20';
  return 'bg-red-900/20 border-critical/20';
}

interface DatabaseHealthCardProps {
  data: HealthCard | null;
}

export function DatabaseHealthCard({ data }: DatabaseHealthCardProps) {
  if (!data) {
    return (
      <div className="card p-4 flex items-center justify-center h-32">
        <p className="text-xs text-surface-4 animate-pulse">Building health card…</p>
      </div>
    );
  }

  const avgScore = data.cards.length
    ? data.cards.reduce((a, c) => a + (c.health_score ?? 0), 0) / data.cards.length
    : 0;

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Heart size={16} className={scoreColor(avgScore)} />
          <h3 className="text-sm font-semibold text-white">Database Health Card</h3>
        </div>
        <div className="text-right">
          <span className={clsx('text-2xl font-bold mono', scoreColor(avgScore))}>
            {avgScore.toFixed(1)}
          </span>
          <span className="text-xs text-surface-4 ml-1">/10</span>
        </div>
      </div>

      <div className="space-y-2">
        {data.cards.map((c) => (
          <CollectionHealth key={c.collection} card={c} />
        ))}
      </div>

      <p className="text-xs text-surface-4">
        Built at startup · {data.db_name} · {data.cards.length} collections
      </p>
    </div>
  );
}

function CollectionHealth({ card: c }: { card: CollectionHealthCard }) {
  return (
    <div className={clsx('rounded-lg border p-3 space-y-1.5', scoreBorderBg(c.health_score))}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-white mono">{c.collection}</span>
        <span className={clsx('text-sm font-bold mono', scoreColor(c.health_score))}>
          {(c.health_score ?? 0).toFixed(1)}/10
        </span>
      </div>

      {/* Score bar */}
      <div className="h-1.5 bg-surface-2 rounded-full overflow-hidden">
        <div
          className={clsx(
            'h-full rounded-full transition-all duration-700',
            c.health_score >= 8 ? 'bg-success'
              : c.health_score >= 5 ? 'bg-warning'
              : 'bg-critical',
          )}
          style={{ width: `${(c.health_score ?? 0) * 10}%` }}
        />
      </div>

      {/* Stats — using correct field names from backend */}
      <div className="flex gap-3 text-xs text-surface-4 mono flex-wrap">
        {c.doc_count != null && <span>{c.doc_count.toLocaleString()} docs</span>}
        {c.p99_size_kb != null && <span>P99: {c.p99_size_kb.toFixed(0)} KB</span>}
        {c.index_count != null && <span>{c.index_count} indexes</span>}
      </div>

      {/* Issues list */}
      {c.issues?.length > 0 && (
        <div className="space-y-0.5">
          {c.issues.slice(0, 3).map((issue, i) => (
            <div key={i} className="flex items-start gap-1">
              <AlertCircle size={10} className="text-warning mt-0.5 shrink-0" />
              <span className="text-xs text-gray-400">{issue}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
