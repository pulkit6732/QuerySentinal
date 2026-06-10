'use client';
/**
 * AnomalyFeed.tsx — Live scrolling feed of anomaly events.
 *
 * Receives anomalies pushed via SSE from the Change Stream watcher.
 * New items animate in from the top.
 * Click any row to open the IncidentModal.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { formatDistanceToNowStrict } from 'date-fns';
import { AlertTriangle, Database, Zap, TrendingUp, Activity } from 'lucide-react';
import clsx from 'clsx';
import type { Anomaly, AnomalyType } from '@/lib/api';
import { useSSEStream } from '@/hooks/useSSEStream';

const TYPE_META: Record<AnomalyType, { icon: React.ReactNode; label: string; color: string }> = {
  DOC_SIZE_SPIKE:          { icon: <Database size={14} />,    label: 'Doc Size Spike',    color: 'text-orange-400' },
  SCHEMA_DRIFT:            { icon: <AlertTriangle size={14} />, label: 'Schema Drift',    color: 'text-yellow-400' },
  WRITE_VOLUME_SPIKE:      { icon: <TrendingUp size={14} />,  label: 'Write Spike',       color: 'text-red-400' },
  SEMANTIC_VELOCITY_SPIKE: { icon: <Zap size={14} />,         label: 'Semantic Velocity', color: 'text-purple-400' },
};

interface AnomalyFeedProps {
  initialAnomalies: Anomaly[];
  onSelect:         (anomaly: Anomaly) => void;
}

export function AnomalyFeed({ initialAnomalies, onSelect }: AnomalyFeedProps) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>(initialAnomalies.slice(0, 50));
  const [newIds,    setNewIds]    = useState<Set<string>>(new Set());
  const listRef = useRef<HTMLDivElement>(null);

  const handleAnomaly = useCallback((a: Anomaly) => {
    setAnomalies(prev => {
      // Deduplicate by _id
      if (prev.some(x => x._id === a._id)) return prev;
      return [a, ...prev].slice(0, 100);
    });
    setNewIds(prev => new Set(prev).add(a._id));
    // Remove 'new' highlight after 3s
    setTimeout(() => {
      setNewIds(prev => {
        const next = new Set(prev);
        next.delete(a._id);
        return next;
      });
    }, 3000);
  }, []);

  const { status, lastPing, eventCount } = useSSEStream({ onAnomaly: handleAnomaly });

  // Poll /api/anomalies as the source of truth every 3s. This guarantees the
  // feed reflects the database even if SSE is buffered by the dev proxy, AND
  // clears stale entries after a reset_demo (no manual refresh needed).
  const seenRef = useRef<Set<string>>(new Set(initialAnomalies.map(a => a._id)));
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const res = await fetch('/api/anomalies?limit=50', { cache: 'no-store' });
        if (!res.ok) return;
        const json = await res.json();
        const list: Anomaly[] = json.anomalies || [];
        if (!alive) return;
        // Highlight any _id we haven't seen before
        const fresh = list.filter(a => !seenRef.current.has(a._id));
        if (fresh.length) {
          setNewIds(prev => {
            const next = new Set(prev);
            fresh.forEach(a => next.add(a._id));
            return next;
          });
          fresh.forEach(a => seenRef.current.add(a._id));
          setTimeout(() => {
            setNewIds(prev => {
              const next = new Set(prev);
              fresh.forEach(a => next.delete(a._id));
              return next;
            });
          }, 3000);
        }
        setAnomalies(list.slice(0, 100));   // server is source of truth
      } catch {
        /* ignore transient fetch errors */
      }
    };
    poll();
    const id = setInterval(poll, 3000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <div className="card flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-surface-3">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-mongo-bright" />
          <h2 className="text-sm font-semibold text-white">Live Anomaly Feed</h2>
          {anomalies.length > 0 && (
            <span className="mono text-xs text-surface-4 bg-surface-3 rounded px-1.5 py-0.5">
              {anomalies.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {status === 'live' && <span className="live-dot" />}
          {status === 'connecting' && (
            <span className="text-xs text-yellow-400 animate-pulse">connecting…</span>
          )}
          {status === 'error' && (
            <span className="text-xs text-critical">reconnecting…</span>
          )}
          {lastPing && (
            <span className="text-xs text-surface-4 mono">
              {eventCount} events
            </span>
          )}
        </div>
      </div>

      {/* Feed list */}
      <div ref={listRef} className="flex-1 overflow-y-auto min-h-0">
        {anomalies.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-surface-4">
            <Activity size={24} className="opacity-40" />
            <p className="text-sm">Watching for anomalies…</p>
          </div>
        ) : (
          anomalies.map((a) => (
            <AnomalyRow
              key={a._id}
              anomaly={a}
              isNew={newIds.has(a._id)}
              onClick={() => onSelect(a)}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ── Row component ─────────────────────────────────────────────────────────────

function AnomalyRow({
  anomaly,
  isNew,
  onClick,
}: {
  anomaly: Anomaly;
  isNew:   boolean;
  onClick: () => void;
}) {
  const meta = TYPE_META[anomaly.anomaly_type] ?? {
    icon: <Activity size={14} />, label: anomaly.anomaly_type, color: 'text-gray-400',
  };

  // Ensure the timestamp is parsed as UTC. Backend serializes naive-UTC; if the
  // string carries no timezone, append 'Z' so the browser doesn't assume local.
  const _ts = anomaly.detected_at;
  const _utc = _ts && !/[Z+]|[+-]\d\d:\d\d$/.test(_ts) ? _ts + 'Z' : _ts;
  const ago = _utc
    ? formatDistanceToNowStrict(new Date(_utc), { addSuffix: true })
    : '—';

  return (
    <button
      onClick={onClick}
      className={clsx(
        'w-full text-left px-4 py-3 border-b border-surface-2',
        'hover:bg-surface-2 transition-colors cursor-pointer',
        'flex items-start gap-3',
        isNew && 'animate-slide-in bg-mongo-dark/20',
      )}
    >
      {/* Severity indicator */}
      <div className="mt-0.5 shrink-0">
        {anomaly.severity === 'CRITICAL' ? (
          <span className="block w-2 h-2 rounded-full bg-critical shadow-glow-red mt-1" />
        ) : (
          <span className="block w-2 h-2 rounded-full bg-warning mt-1" />
        )}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={clsx('flex items-center gap-1 text-xs font-medium', meta.color)}>
            {meta.icon} {meta.label}
          </span>
          <span className="text-xs text-surface-4 mono">{anomaly.collection_name}</span>
          <span className={clsx(
            'ml-auto text-xs font-bold mono',
            anomaly.severity === 'CRITICAL' ? 'text-critical' : 'text-warning',
          )}>
            Z={anomaly.z_score?.toFixed(1)}
          </span>
        </div>
        <p className="text-xs text-gray-300 mt-0.5 line-clamp-2 leading-relaxed">
          {anomaly.description}
        </p>
        <p className="text-xs text-surface-4 mono mt-1">{ago}</p>
      </div>
    </button>
  );
}
