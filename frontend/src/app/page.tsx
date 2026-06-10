'use client';
/**
 * page.tsx — QUERYSENTINEL main dashboard.
 *
 * Layout (3-column on large screens):
 *   Left column  (30%): Live Anomaly Feed (SSE)
 *   Center column (40%): Semantic Velocity + Runway + Database Health Card
 *   Right column (30%): System status + Inject Test button + Robustness signals
 *
 * Incident modal opens on any anomaly row click or incident card click.
 *
 * All data fetched server-side on first load; live updates via SSE.
 */
import { useCallback, useEffect, useState } from 'react';
import { formatDistanceToNowStrict } from 'date-fns';
import {
  Shield, RefreshCw, Zap, Database,
  AlertTriangle, Activity, CheckCircle2, XCircle, Loader2,
} from 'lucide-react';
import clsx from 'clsx';

import { api, type Anomaly, type Incident, type HealthCard,
         type RunwayResult, type SemanticVelocityResult } from '@/lib/api';
import { AnomalyFeed }            from '@/components/AnomalyFeed';
import { IncidentModal }           from '@/components/IncidentModal';
import { SemanticVelocityCard }    from '@/components/SemanticVelocityCard';
import { RunwayCard }              from '@/components/RunwayCard';
import { DatabaseHealthCard }      from '@/components/DatabaseHealthCard';
import { DottedSurface }           from '@/components/ui/dotted-surface';

// ── Page state ────────────────────────────────────────────────────────────────

interface PageData {
  anomalies:  Anomaly[];
  incidents:  Incident[];
  runway:     Record<string, RunwayResult>;
  velocity:   Record<string, SemanticVelocityResult>;
  healthCard: HealthCard | null;
}

export default function DashboardPage() {
  const [data, setData] = useState<PageData>({
    anomalies: [], incidents: [], runway: {}, velocity: {}, healthCard: null,
  });
  const [loading, setLoading] = useState(true);
  const [health,  setHealth]  = useState<Record<string, unknown> | null>(null);

  // Selected anomaly + its incident report for modal
  const [selectedAnomaly,  setSelectedAnomaly]  = useState<Anomaly | null>(null);
  const [selectedIncident, setSelectedIncident] = useState<Incident | null>(null);

  // Inject test state
  const [injecting,    setInjecting]    = useState(false);
  const [injectResult, setInjectResult] = useState<string | null>(null);

  // ── Initial data load ────────────────────────────────────────────────────
  useEffect(() => {
    async function load() {
      try {
        const [
          anomaliesRes,
          incidentsRes,
          runwayRes,
          velocityRes,
          healthCardRes,
          healthRes,
        ] = await Promise.allSettled([
          api.anomalies(50),
          api.incidents(20),
          api.runway(),
          api.semanticVelocity(),
          api.healthCard(),
          api.health(),
        ]);

        setData({
          anomalies:  anomaliesRes.status  === 'fulfilled' ? anomaliesRes.value.anomalies   : [],
          incidents:  incidentsRes.status  === 'fulfilled' ? incidentsRes.value.incidents    : [],
          runway:     runwayRes.status     === 'fulfilled' ? runwayRes.value.runway          : {},
          velocity:   velocityRes.status   === 'fulfilled' ? velocityRes.value.velocity      : {},
          healthCard: healthCardRes.status === 'fulfilled' ? healthCardRes.value             : null,
        });

        if (healthRes.status === 'fulfilled') {
          setHealth(healthRes.value.components as Record<string, unknown>);
        }
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  // ── Handle anomaly click (open modal + find incident) ────────────────────
  const handleSelectAnomaly = useCallback(async (anomaly: Anomaly) => {
    setSelectedAnomaly(anomaly);
    setSelectedIncident(null);

    // Find matching incident
    const incident = data.incidents.find(
      i => i.anomaly_event?.['_id'] === anomaly._id ||
           i.collection_name === anomaly.collection_name
    );
    if (incident) {
      setSelectedIncident(incident);
    } else {
      // Try fetching fresh
      try {
        const res = await api.incidents(20);
        const fresh = res.incidents.find(
          i => i.collection_name === anomaly.collection_name
        );
        if (fresh) setSelectedIncident(fresh);
      } catch (_) {}
    }
  }, [data.incidents]);

  // ── Inject test anomaly ───────────────────────────────────────────────────
  async function handleInjectTest() {
    setInjecting(true);
    setInjectResult(null);
    try {
      const res = await api.injectTest();
      setInjectResult(`✓ ${res.approx_size_kb} KB document injected (ID: ${res.document_id.slice(-8)})`);
    } catch (err) {
      setInjectResult(`✗ ${err instanceof Error ? err.message : 'Failed'}`);
    } finally {
      setInjecting(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-0">
        <div className="flex flex-col items-center gap-4">
          <Shield size={48} className="text-mongo-bright animate-pulse" />
          <p className="text-sm text-surface-4">Connecting to QUERYSENTINEL…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-black flex flex-col relative">
      <DottedSurface />

      {/* ── Top bar ──────────────────────────────────────────────────────── */}
      <header className="border-b border-surface-3 bg-surface-1 px-6 py-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <Shield size={20} className="text-mongo-bright" />
          <span className="font-bold text-white tracking-wide">QUERYSENTINEL</span>
          <span className="text-xs text-surface-4 hidden sm:block">
            Live MongoDB Anomaly Intelligence
          </span>
        </div>
        <div className="flex items-center gap-3">
          <HealthPill health={health} />
          <button
            onClick={handleInjectTest}
            disabled={injecting}
            title="Inject a 300KB test document into the movies collection to trigger a live anomaly + the agent pipeline"
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold',
              'bg-black/60 border border-white/60 text-white',
              'hover:bg-white/10 transition-colors',
              injecting && 'opacity-60 cursor-not-allowed',
            )}
          >
            {injecting
              ? <><Loader2 size={12} className="animate-spin" /> Injecting…</>
              : <><Zap size={12} /> Inject Test Anomaly</>
            }
          </button>
        </div>
      </header>

      {/* Inject result toast */}
      {injectResult && (
        <div className={clsx(
          'mx-6 mt-3 px-4 py-2 rounded-lg text-xs font-medium border animate-fade-in',
          injectResult.startsWith('✓')
            ? 'bg-green-900/20 border-success/30 text-success'
            : 'bg-red-900/20 border-critical/30 text-critical',
        )}>
          {injectResult}
          <span className="text-surface-4 ml-2">
            — Watch the feed for the anomaly event (~200ms)
          </span>
        </div>
      )}

      {/* ── Main 3-column grid ────────────────────────────────────────────── */}
      <main className="flex-1 grid grid-cols-1 lg:grid-cols-[30%_1fr_28%] gap-4 p-4 overflow-hidden min-h-0">

        {/* LEFT: Live anomaly feed */}
        <AnomalyFeed
          initialAnomalies={data.anomalies}
          onSelect={handleSelectAnomaly}
        />

        {/* CENTER: Analytics cards */}
        <div className="space-y-4 overflow-y-auto min-h-0">
          <SemanticVelocityCard data={data.velocity} />
          <RunwayCard data={data.runway} />
        </div>

        {/* RIGHT: Health + system status */}
        <div className="space-y-4 overflow-y-auto min-h-0">
          <DatabaseHealthCard data={data.healthCard} />
          <SystemStatusCard health={health} />
          <ProductionRobustnessCard />
        </div>
      </main>

      {/* ── Incident Modal ───────────────────────────────────────────────── */}
      {selectedAnomaly && (
        <IncidentModal
          anomaly={selectedAnomaly}
          incident={selectedIncident}
          onClose={() => { setSelectedAnomaly(null); setSelectedIncident(null); }}
        />
      )}
    </div>
  );
}

// ── Utility components ────────────────────────────────────────────────────────

function HealthPill({ health }: { health: Record<string, unknown> | null }) {
  if (!health) return null;
  const ok = health['mongodb'] === 'ok' && health['vector_search'] === 'ok';
  return (
    <div className={clsx(
      'flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border',
      ok
        ? 'bg-green-900/20 border-success/30 text-success'
        : 'bg-yellow-900/20 border-warning/30 text-warning',
    )}>
      {ok ? <CheckCircle2 size={11} /> : <AlertTriangle size={11} />}
      {ok ? 'All Systems OK' : 'Degraded'}
    </div>
  );
}

function SystemStatusCard({ health }: { health: Record<string, unknown> | null }) {
  if (!health) return null;

  const items = [
    { label: 'MongoDB Atlas',    status: health['mongodb']          === 'ok' },
    { label: 'Vector Search',    status: health['vector_search']    === 'ok' },
    { label: 'Change Streams',   status: Object.keys(health['stream_watchers'] as object || {}).length > 0 },
    { label: 'Failed Events',    status: (health['failed_events_pending'] as number) === 0 },
    { label: 'Queue Depth',      status: true,
      note: `${health['queue_depth'] as number ?? 0} pending` },
  ];

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Activity size={14} className="text-surface-4" />
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          System Status
        </h3>
      </div>
      {items.map(({ label, status, note }) => (
        <div key={label} className="flex items-center justify-between">
          <span className="text-xs text-gray-400">{label}</span>
          <div className="flex items-center gap-1.5">
            {note && <span className="text-xs text-surface-4 mono">{note}</span>}
            {status
              ? <CheckCircle2 size={13} className="text-success" />
              : <XCircle      size={13} className="text-critical" />
            }
          </div>
        </div>
      ))}
    </div>
  );
}

function ProductionRobustnessCard() {
  const features = [
    { label: 'Persistent Resume Token',    desc: 'Zero event loss on restart' },
    { label: 'Circuit Breaker (3 trips)',  desc: 'Falls back to rule-based alerts' },
    { label: 'Dead Letter Queue',          desc: 'Failed events retry up to 3×' },
    { label: 'Human-in-the-Loop Gate',     desc: 'APPROVE before any MCP write' },
    { label: 'Atlas Stream Processing',    desc: '$tumblingWindow 60s write rate' },
    { label: 'voyage-4 Auto-Embedding',    desc: 'No embedding code — Atlas manages it' },
  ];

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Shield size={14} className="text-mongo-bright" />
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">
          Production Architecture
        </h3>
      </div>
      <div className="space-y-2">
        {features.map(({ label, desc }) => (
          <div key={label} className="flex items-start gap-2">
            <span className="text-mongo-light text-xs mt-0.5 shrink-0">▸</span>
            <div>
              <p className="text-xs font-medium text-white">{label}</p>
              <p className="text-xs text-surface-4">{desc}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
