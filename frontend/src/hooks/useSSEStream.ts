/**
 * useSSEStream.ts — Server-Sent Events hook for live anomaly feed.
 *
 * Connects to /api/stream on mount.
 * Reconnects with exponential backoff on disconnect.
 * Dispatches 'anomaly' events to the provided callback.
 * Surfaces connection state (connecting | live | error | disconnected).
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import type { Anomaly } from '@/lib/api';

type StreamStatus = 'connecting' | 'live' | 'error' | 'disconnected';

interface UseSSEStreamOptions {
  onAnomaly: (anomaly: Anomaly) => void;
  url?:      string;
}

export function useSSEStream({ onAnomaly, url = '/api/stream' }: UseSSEStreamOptions) {
  const [status,       setStatus]       = useState<StreamStatus>('connecting');
  const [lastPing,     setLastPing]     = useState<Date | null>(null);
  const [eventCount,   setEventCount]   = useState(0);

  const esRef          = useRef<EventSource | null>(null);
  const retryCountRef  = useRef(0);
  const retryTimerRef  = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef     = useRef(true);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    setStatus('connecting');

    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener('anomaly', (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data = JSON.parse(e.data) as Anomaly;
        onAnomaly(data);
        setEventCount(c => c + 1);
        setLastPing(new Date());
        retryCountRef.current = 0;   // reset backoff on successful event
      } catch (err) {
        console.warn('[SSE] Failed to parse anomaly event:', err);
      }
    });

    es.addEventListener('keepalive', () => {
      if (!mountedRef.current) return;
      setLastPing(new Date());
      setStatus('live');
    });

    es.onopen = () => {
      if (!mountedRef.current) return;
      setStatus('live');
      retryCountRef.current = 0;
    };

    es.onerror = () => {
      if (!mountedRef.current) return;
      es.close();
      esRef.current = null;
      setStatus('error');

      // Exponential backoff: 1s, 2s, 4s, 8s, max 30s
      const delay = Math.min(1000 * Math.pow(2, retryCountRef.current), 30_000);
      retryCountRef.current += 1;

      retryTimerRef.current = setTimeout(() => {
        if (mountedRef.current) connect();
      }, delay);
    };
  }, [onAnomaly, url]);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [connect]);

  const disconnect = useCallback(() => {
    mountedRef.current = false;
    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    esRef.current?.close();
    esRef.current = null;
    setStatus('disconnected');
  }, []);

  return { status, lastPing, eventCount, disconnect };
}
