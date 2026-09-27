import { useState, useEffect } from 'react';
import './SimulationControl.css';

/**
 * SimulationControl Component
 * 
 * Provides controls for starting/stopping the Wakashio oil spill simulation
 * and displays real-time status updates including fusion-specific progress.
 * 
 * Features:
 * - Start/Stop button with visual state feedback
 * - Progress bar (0-100%)
 * - Real-time statistics (vessels, anomalies, spills)
 * - Fusion-specific status (SAR fetched, EO fetched, fusion confidence)
 * - Stage-specific messages (fetching, processing, complete)
 */
export default function SimulationControl({ onSimulationStart, onSimulationComplete }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Poll status every 2 seconds when simulation is running
  useEffect(() => {
    let interval;
    
    const fetchStatus = async () => {
      try {
        const response = await fetch('/api/simulate/status');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        setStatus(data);
        
        // Notify parent component of simulation state changes
        if (data.running && data.progress_pct === 0) {
          onSimulationStart?.();
        } else if (!data.running && data.progress_pct === 100) {
          onSimulationComplete?.(data);
        }
      } catch (err) {
        console.error('Failed to fetch simulation status:', err);
        setError('Failed to fetch status');
      }
    };

    // Initial fetch
    fetchStatus();

    // Poll while simulation might be running
    if (!status || status.running) {
      interval = setInterval(fetchStatus, 2000);
    }

    return () => {
      if (interval) clearInterval(interval);
    };
  }, [status?.running, onSimulationStart, onSimulationComplete]);

  const handleStart = async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/simulate/oil-spill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${response.status}`);
      }

      const data = await response.json();
      console.log('Simulation started:', data);
      
      // Status will be updated by polling
    } catch (err) {
      console.error('Failed to start simulation:', err);
      setError(err.message || 'Failed to start simulation');
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/simulate/stop', {
        method: 'POST',
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${response.status}`);
      }

      console.log('Simulation stopped');
      
      // Status will be updated by polling
    } catch (err) {
      console.error('Failed to stop simulation:', err);
      setError(err.message || 'Failed to stop simulation');
    } finally {
      setLoading(false);
    }
  };

  // Derive fusion stage message
  const getFusionStageMessage = () => {
    if (!status) return null;
    
    const { fusion_status, sar_fetched, eo_fetched } = status;
    
    if (fusion_status === 'idle') return null;
    
    if (!sar_fetched && !eo_fetched) {
      return '🛰️ Fetching Sentinel-1 SAR + Sentinel-2 optical imagery...';
    }
    
    if (sar_fetched && !eo_fetched) {
      return '🛰️ SAR fetched, waiting for optical imagery...';
    }
    
    if (fusion_status === 'processing' || fusion_status === 'sar_only') {
      return '🔬 Processing SAR imagery...';
    }
    
    if (sar_fetched && eo_fetched && fusion_status !== 'complete') {
      return '🔬 Fusing SAR + EO models...';
    }
    
    if (fusion_status === 'complete' && status.fusion_confidence) {
      return `✅ Fusion complete | Confidence: ${(status.fusion_confidence * 100).toFixed(0)}%`;
    }
    
    return null;
  };

  // Derive confidence badge color
  const getConfidenceBadgeClass = () => {
    if (!status?.fusion_confidence) return '';
    
    const confidence = status.fusion_confidence * 100;
    
    if (confidence >= 85) return 'confidence-high';
    if (confidence >= 70) return 'confidence-moderate';
    return 'confidence-low';
  };

  const isRunning = status?.running ?? false;
  const progress = status?.progress_pct ?? 0;
  const fusionMessage = getFusionStageMessage();

  return (
    <div className="simulation-control">
      {error && (
        <div className="simulation-error">
          <span className="error-icon">⚠️</span>
          {error}
        </div>
      )}

      <button
        className={`simulation-button ${isRunning ? 'running' : 'idle'} ${loading ? 'loading' : ''}`}
        onClick={isRunning ? handleStop : handleStart}
        disabled={loading}
      >
        <span className="button-icon">
          {loading ? '⏳' : isRunning ? '⏹' : '▶'}
        </span>
        <span className="button-text">
          {loading
            ? 'Processing...'
            : isRunning
            ? 'Stop Simulation'
            : 'Simulate Oil Spill Demo'}
        </span>
      </button>

      {isRunning && status && (
        <div className="simulation-status">
          {/* Progress Bar */}
          <div className="progress-container">
            <div className="progress-bar">
              <div 
                className="progress-fill" 
                style={{ width: `${progress}%` }}
              >
                {progress > 10 && <span className="progress-text">{progress}%</span>}
              </div>
            </div>
          </div>

          {/* Statistics Grid */}
          <div className="stats-grid">
            <div className="stat-item">
              <span className="stat-label">Vessels Active</span>
              <span className="stat-value">{status.vessels_active}</span>
            </div>
            
            <div className="stat-item">
              <span className="stat-label">Injected</span>
              <span className="stat-value">{status.vessels_injected}</span>
            </div>
            
            <div className="stat-item highlight">
              <span className="stat-label">⚠️ Flagged</span>
              <span className="stat-value">{status.vessels_flagged}</span>
            </div>
            
            <div className="stat-item">
              <span className="stat-label">Anomalies</span>
              <span className="stat-value">{status.anomalies_detected}</span>
            </div>
            
            <div className="stat-item spills">
              <span className="stat-label">🛢️ Spills</span>
              <span className="stat-value">{status.spills_detected}</span>
            </div>
          </div>

          {/* Fusion Status */}
          {fusionMessage && (
            <div className="fusion-status">
              <div className="fusion-message">{fusionMessage}</div>
              
              {(status.sar_fetched || status.eo_fetched) && (
                <div className="fusion-indicators">
                  <div className={`fusion-indicator ${status.sar_fetched ? 'complete' : 'pending'}`}>
                    <span className="indicator-icon">{status.sar_fetched ? '✓' : '○'}</span>
                    <span className="indicator-label">SAR</span>
                  </div>
                  
                  <div className={`fusion-indicator ${status.eo_fetched ? 'complete' : 'pending'}`}>
                    <span className="indicator-icon">{status.eo_fetched ? '✓' : '○'}</span>
                    <span className="indicator-label">EO</span>
                  </div>
                  
                  {status.fusion_confidence && (
                    <div className={`fusion-indicator complete ${getConfidenceBadgeClass()}`}>
                      <span className="indicator-icon">🔬</span>
                      <span className="indicator-label">
                        {(status.fusion_confidence * 100).toFixed(0)}%
                      </span>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Elapsed Time */}
          <div className="elapsed-time">
            Elapsed: {Math.floor(status.elapsed_seconds / 60)}m {status.elapsed_seconds % 60}s
          </div>
        </div>
      )}

      {/* Completed State */}
      {!isRunning && status?.progress_pct === 100 && (
        <div className="simulation-complete">
          <span className="complete-icon">✅</span>
          <span className="complete-text">
            Simulation Complete | {status.spills_detected} spill(s) detected
            {status.fusion_confidence && 
              ` | ${(status.fusion_confidence * 100).toFixed(0)}% fusion confidence`
            }
          </span>
        </div>
      )}
    </div>
  );
}
