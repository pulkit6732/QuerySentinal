'use client';
/**
 * RunwayCard.tsx — Incident runway prediction bar.
 *
 * Linear extrapolation on ASP stream_stats rolling write rate
 * predicts time-to-critical for each collection.
 *
 * Shows:
 *  - Horizontal fuel-gauge bar per collection
 *  - Time-to-critical ETA or "SAFE"
 *  - Write rate trend (increasing / stable / decreasing)
 */
import clsx from 'clsx';
import { Timer, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import type { RunwayResult } from '@/lib/api';
import { formatDistanceToNowStrict, addMinutes } from 'date-fns';

const TREND_ICON = {
  increasing: <TrendingUp  size={12} className="text-critical" />,
  stable:     <Minus       size={12} className="text-gray-400" />,
  decreasing: <TrendingDown size={12} className="text-success" />,
};

interface RunwayCardProps {
  data: Record<string, RunwayResult>;
}

export function RunwayCard({ data }: RunwayCardProps) {
  const entries = Object.values(data);

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-center gap-2">
        <Timer size={16} className="text-orange-400" />
        <h3 className="text-sm font-semibold text-white">Incident Runway</h3>
        <span className="text-xs text-surface-4 ml-1">via Atlas Stream Processing</span>
      </div>

      {entries.length === 0 ? (
        <p className="text-xs text-surface-4 text-center py-4">No runway data yet.</p>
      ) : (
        <div className="space-y-3">
          {entries.map((r) => (
            <RunwayRow key={r.collection_name} result={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function RunwayRow({ result: r }: { result: RunwayResult }) {
  // Build fill percentage (0 = safe, 100 = critical)
  const maxMinutes = 240;  // 4 hours = full bar
  const minutesLeft = r.minutes_to_critical ?? maxMinutes;
  const fillPct  = Math.max(0, Math.min(100, ((maxMinutes - minutesLeft) / maxMinutes) * 100));

  const isCritical  = minutesLeft != null && minutesLeft < 30;
  const isWarning   = minutesLeft != null && minutesLeft < 90;
  const isSafe      = minutesLeft == null || minutesLeft >= maxMinutes;

  const eta = r.projected_critical_at
    ? formatDistanceToNowStrict(new Date(r.projected_critical_at), { addSuffix: true })
    : null;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-white mono">{r.collection_name}</span>
          {TREND_ICON[r.trend] ?? TREND_ICON.stable}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-surface-4 mono">
            {r.current_rate_per_min?.toFixed(1)}/min
          </span>
          {isSafe ? (
            <span className="badge-ok">SAFE</span>
          ) : isCritical ? (
            <span className="badge-critical">{eta ?? `${minutesLeft}m`}</span>
          ) : (
            <span className="badge-warning">{eta ?? `${minutesLeft}m`}</span>
          )}
        </div>
      </div>

      {/* Runway bar */}
      <div className="h-2 bg-surface-2 rounded-full overflow-hidden">
        <div
          className={clsx(
            'h-full rounded-full transition-all duration-700',
            isCritical ? 'bg-critical shadow-glow-red'
              : isWarning ? 'bg-warning shadow-glow-yellow'
              : 'bg-mongo-light',
          )}
          style={{ width: `${fillPct}%` }}
        />
      </div>

      {/* Sub-label */}
      <p className="text-xs text-surface-4">
        {isSafe
          ? `No critical event projected within 4 hours`
          : `Projected critical event ${eta} — ${r.confidence
              ? `${(r.confidence * 100).toFixed(0)}% confidence`
              : ''}`
        }
      </p>
    </div>
  );
}
