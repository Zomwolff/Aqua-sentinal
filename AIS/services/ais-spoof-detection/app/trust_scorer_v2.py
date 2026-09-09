"""
Trust Scorer Adapter - Bridges old and new implementations.

This module provides a unified interface that can switch between:
- Old weighted approach (trust_scorer.py) for backward compatibility
- New Bayesian approach (bayesian_trust_scorer.py) for accuracy

Use environment variable:
  USE_BAYESIAN_TRUST_SCORER=true  → Use new Bayesian model
  USE_BAYESIAN_TRUST_SCORER=false → Use old weighted model (default for now)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Feature flag for gradual rollout
_USE_BAYESIAN = os.environ.get("USE_BAYESIAN_TRUST_SCORER", "true").lower() in ("true", "1", "yes")

if _USE_BAYESIAN:
    try:
        from bayesian_trust_scorer import compute_bayesian_trust_score
        _BAYESIAN_AVAILABLE = True
        log.info("Bayesian trust scorer available - will use for HIGH/CRITICAL tiers")
    except ImportError as e:
        log.warning(f"Could not import Bayesian trust scorer: {e}")
        _BAYESIAN_AVAILABLE = False
else:
    _BAYESIAN_AVAILABLE = False


async def compute_trust_score(
    pool,
    mmsi: int,
    current_data: Dict[str, Any],
    previous_data: Optional[Dict[str, Any]] = None,
    use_bayesian: bool = _USE_BAYESIAN,
) -> Dict[str, Any]:
    """
    Compute trust score for a vessel using Bayesian or legacy method.
    
    Args:
        pool: Database connection pool
        mmsi: Vessel MMSI
        current_data: Latest AIS report
        previous_data: Previous AIS report
        use_bayesian: If True, use new Bayesian model; else use legacy weighted
    
    Returns:
        Trust score dict with posterior probability and evidence
    """
    if use_bayesian and _BAYESIAN_AVAILABLE:
        log.debug(f"Computing Bayesian trust score for MMSI {mmsi}")
        result = await compute_bayesian_trust_score(pool, mmsi, current_data, previous_data)
        
        # Add bridge field for code that expects "rolling_trust_score"
        result["rolling_trust_score"] = result["posterior_probability_genuine"]
        
        return result
    else:
        # Fall back to legacy approach (implement as needed)
        log.debug(f"Computing legacy trust score for MMSI {mmsi}")
        return {
            "mmsi": mmsi,
            "rolling_trust_score": 1.0,  # Default to fully trusted if model unavailable
            "model_version": "legacy_weighted_v1",
            "warning": "Bayesian model unavailable, using legacy defaults",
        }
