import { useState, useEffect, useRef } from 'react';
import './SimulationControl.css';

interface SimulationStatus {
  running: boolean;
  scenario: string;
  progress_pct: number;
  vessels_injected: number;
  records_processed: number;
  total_records: number;
  db_write_count: number;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
}

interface SimulationControlProps {
  onSimulationStart?: () => void;
  onSimulationComplete?: () => void;
}

export default function SimulationControl({
  onSimulationStart,
  onSimulationComplete,
}: SimulationControlProps) {
  const [status, setStatus] = useState<SimulationStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justCompleted, setJustCompleted] = useState(false);
  const prevRunning = useRef<boolean>(false);

  // Poll every 3 s while running, every 10 s otherwise
  useEffect(() => {
    let id: ReturnType<typeof setTimeout>;

    const poll = async () => {
      try {
        const res = await fetch('/api/simulate/status');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: SimulationStatus = await res.json();
        setStatus(data);
        setError(null);

        if (data.running && !prevRunning.current) {
          prevRunning.current = true;
          onSimulationStart?.();
        }
        if (!data.running && prevRunning.current) {
          prevRunning.current = false;
          setJustCompleted(true);
          onSimulationComplete?.();
          setTimeout(() => setJustCompleted(false), 8000);
        }
      } catch (err) {
        // Silently ignore — don't show poll errors unless user just triggered
      }

      id = setTimeout(poll, status?.running ? 3000 : 10000);
    };

    poll();
    return () => clearTimeout(id);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleStart = async () => {
    setLoading(true);
    setError(null);
    setJustCompleted(false);

    try {
      const res = await fetch('/api/simulate/oil-spill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      // Status will be picked up by the poller
    } catch (err: any) {
      setError(err.message || 'Failed to start simulation');
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    try {
      await fetch('/api/simulate/stop', { method: 'POST' });
    } catch {
      // ignore
    }
  };

  const isRunning = status?.running ?? false;
  const progress = status?.progress_pct ?? 0;

  // Elapsed wall-clock seconds since simulation started
  const elapsedSec = status?.started_at
    ? Math.floor((Date.now() - new Date(status.started_at).getTime()) / 1000)
    : 0;
  const elapsedStr = `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s`;

  return (
    <div className="simulation-control">
      {error && (
        <div className="simulation-error">
          <span className="error-icon">⚠️</span>
          {error}
        </div>
      )}

      {justCompleted && (
        <div className="simulation-complete-banner">
          ✅ Simulation complete — MV WAKASHIO flagged CRITICAL on the map
        </div>
      )}

      <button
        className={`simulation-button ${isRunning ? 'running' : 'idle'} ${loading ? 'loading' : ''}`}
        onClick={handleStart}
        disabled={loading || isRunning}
        title="Stream realistic Wakashio oil-spill AIS data (6 714 records, 12 vessels, 120× speed)"
      >
        <span className="button-icon">
          {loading ? '⏳' : isRunning ? '🛢️' : '🛢️'}
        </span>
        <span className="button-text">
          {loading
            ? 'Starting…'
            : isRunning
            ? 'Simulation Running…'
            : 'Simulate Oil Spill'}
        </span>
      </button>

      {isRunning && status && (
        <>
          <div className="simulation-status">
            <div className="progress-container">
              <div className="progress-bar">
                <div
                  className="progress-fill"
                  style={{ width: `${progress}%` }}
                >
                  {progress > 8 && (
                    <span className="progress-text">{progress}%</span>
                  )}
                </div>
              </div>
            </div>

            <div className="simulation-stats">
              <span className="stat">
                📡 {status.records_processed.toLocaleString()}/{status.total_records.toLocaleString()} records
              </span>
              <span className="stat">🚢 {status.vessels_injected} vessels</span>
              <span className="stat">💾 {status.db_write_count.toLocaleString()} DB writes</span>
              <span className="stat">⏱ {elapsedStr}</span>
            </div>
          </div>

          <button
            className="simulation-stop-button"
            onClick={handleStop}
            title="Stop the simulation"
          >
            ⏹ Stop
          </button>
        </>
      )}

      {!isRunning && status?.completed_at && !justCompleted && (
        <div className="simulation-done">
          ✅ {status.records_processed.toLocaleString()} records injected •{' '}
          {status.vessels_injected} vessels •{' '}
          {status.db_write_count.toLocaleString()} DB writes
        </div>
      )}
    </div>
  );
}
