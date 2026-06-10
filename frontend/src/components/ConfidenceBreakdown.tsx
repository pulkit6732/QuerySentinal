'use client';
/**
 * ConfidenceBreakdown.tsx — Weighted evidence confidence visualization.
 *
 * Shows four evidence sources with their weights:
 *   Atlas Performance Advisor  35%
 *   Vector Search (voyage-4)   30%
 *   Schema Drift               20%
 *   Z-Score statistical        15%
 *
 * Stacked horizontal bar + score badges.
 */
import clsx from 'clsx';

interface EvidenceSlice {
  label:       string;
  weight:      number;    // 0–1 weight in total score
  score:       number;    // 0–1 evidence quality
  color:       string;    // tailwind bg color
  description: string;
}

interface ConfidenceBreakdownProps {
  rootCause?:    Record<string, unknown>;
  similarScore?: number;
  schemaDrift?:  Record<string, unknown>;
  zScore?:       number;
}

function normalize(val: number, min: number, max: number): number {
  return Math.max(0, Math.min(1, (val - min) / (max - min)));
}

export function ConfidenceBreakdown({
  rootCause,
  similarScore = 0,
  schemaDrift,
  zScore = 0,
}: ConfidenceBreakdownProps) {
  const paScore       = rootCause ? (rootCause['confidence'] as number ?? 0) : 0;
  const schemaScore   = schemaDrift
    ? Math.min(1, ((schemaDrift['added_fields'] as unknown[])?.length || 0) * 0.2 +
                  ((schemaDrift['type_changes'] as unknown[])?.length || 0) * 0.3)
    : 0;
  const zScoreNorm    = normalize(zScore, 2, 10);

  const slices: EvidenceSlice[] = [
    {
      label:       'Performance Advisor',
      weight:      0.35,
      score:       paScore,
      color:       'bg-neutral-300',
      description: `Atlas PA confidence: ${(paScore * 100).toFixed(0)}%`,
    },
    {
      label:       'Vector Search (voyage-4)',
      weight:      0.30,
      score:       similarScore,
      color:       'bg-purple-500',
      description: `Semantic similarity to past incidents: ${(similarScore * 100).toFixed(0)}%`,
    },
    {
      label:       'Schema Drift',
      weight:      0.20,
      score:       schemaScore,
      color:       'bg-yellow-500',
      description: `Structural deviation score: ${(schemaScore * 100).toFixed(0)}%`,
    },
    {
      label:       'Statistical (Z-Score)',
      weight:      0.15,
      score:       zScoreNorm,
      color:       'bg-orange-500',
      description: `Z=${zScore.toFixed(2)} → normalized ${(zScoreNorm * 100).toFixed(0)}%`,
    },
  ];

  const totalConfidence = slices.reduce((acc, s) => acc + s.weight * s.score, 0);

  return (
    <div className="space-y-4">
      {/* Total score */}
      <div className="flex items-end justify-between">
        <div>
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-1">
            Composite Confidence
          </p>
          <div className="flex items-baseline gap-2">
            <span className={clsx(
              'text-3xl font-bold mono',
              totalConfidence >= 0.8 ? 'text-critical'
                : totalConfidence >= 0.6 ? 'text-warning'
                : 'text-mongo-bright',
            )}>
              {(totalConfidence * 100).toFixed(0)}%
            </span>
            <span className="text-xs text-surface-4">
              {totalConfidence >= 0.85 ? 'HIGH' : totalConfidence >= 0.60 ? 'MEDIUM' : 'LOW'} confidence
            </span>
          </div>
        </div>
        <div className="text-right">
          <p className="text-xs text-surface-4">Weighted evidence model</p>
          <p className="text-xs text-surface-4 mono">PA×0.35 + VS×0.30 + SD×0.20 + Z×0.15</p>
        </div>
      </div>

      {/* Stacked bar */}
      <div className="h-3 rounded-full overflow-hidden bg-surface-2 flex">
        {slices.map((s) => (
          <div
            key={s.label}
            className={clsx('h-full transition-all duration-700', s.color)}
            style={{ width: `${s.weight * s.score * 100}%` }}
            title={`${s.label}: ${(s.score * 100).toFixed(0)}% × ${s.weight * 100}% weight`}
          />
        ))}
      </div>

      {/* Evidence table */}
      <div className="space-y-2">
        {slices.map((s) => (
          <div key={s.label} className="flex items-center gap-3">
            {/* Color swatch */}
            <div className={clsx('w-2.5 h-2.5 rounded-sm shrink-0', s.color)} />

            {/* Label */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-white">{s.label}</span>
                <span className="text-xs mono text-gray-400">
                  {(s.score * 100).toFixed(0)}% × {(s.weight * 100).toFixed(0)}%
                </span>
              </div>
              {/* Progress bar */}
              <div className="mt-1 h-1 bg-surface-2 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all duration-500', s.color)}
                  style={{ width: `${s.score * 100}%` }}
                />
              </div>
              <p className="text-xs text-surface-4 mt-0.5">{s.description}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
