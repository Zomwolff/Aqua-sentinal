#!/usr/bin/env python3
"""
scripts/demo_sar_spill.py — End-to-end SAR spill demonstration.

PURPOSE
-------
Exercise the REAL running Docker pipeline for two scenarios:

  RUN A — REAL SENTINEL-1 IMAGERY
      Trigger the existing data-ingestion SAR acquisition (inject_synthetic=false)
      over a Mumbai-area Sentinel-1 date range and observe detector behavior on
      real imagery. A real scene may legitimately yield 0 or 1+ candidates.

  RUN B — CONTROLLED SYNTHETIC VALIDATION
      Trigger the SAME acquisition workflow with inject_synthetic=true and verify
      is_synthetic == true propagates through every pipeline boundary.

This is an integration/demo tool. It does NOT mock data-ingestion, SAR
processing, Redis, PostGIS, lookalike-engine, evidence-fusion, or api-gateway.
It only reads/writes through the real services.

HOW TO RUN (inside the data-ingestion container, which owns GEE creds + /app/shared)
------------------------------------------------------------------------------------
    docker compose cp scripts/demo_sar_spill.py data-ingestion:/app/demo_sar_spill.py
    docker compose exec data-ingestion python /app/demo_sar_spill.py

Running inside the existing data-ingestion container reuses its environment
(GEE_SERVICE_ACCOUNT, /run/secrets/gee-key.json, INJECT_SYNTHETIC) and the
aqua-net Docker network, so it reaches redis/postgres/downstream services by
service name without starting a second copy of any service.

The script triggers acquisition by calling the EXISTING function
app.sar_acquisition.run_sar_acquisition — no new service communication pattern.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# ── Pure helpers (no heavy imports; unit-testable without Docker) ──────────────

DEFAULT_TIMEOUT = 1800  # seconds (30 min) for the whole pipeline to settle


def parse_sar_clean_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Extract scene metadata from a ``sar.clean`` stream entry.

    ``msg`` may be a raw Redis entry ``{id, data}`` or an already-flat dict.
    Returns {scene_id, is_synthetic, acquisition_time}.
    """
    data = msg.get("data", msg) if isinstance(msg, dict) else {}
    meta_raw = data.get("scene_metadata")
    if isinstance(meta_raw, str):
        meta = json.loads(meta_raw)
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}
    return {
        "scene_id": meta.get("scene_id"),
        "is_synthetic": bool(meta.get("is_synthetic", False)),
        "acquisition_time": meta.get("acquisition_time"),
        "raster_path": data.get("raster_path"),
    }


def filter_by_scene(items: List[Dict[str, Any]], scene_id: str,
                    key: str = "scene_id") -> List[Dict[str, Any]]:
    """Return only the items whose ``key`` equals ``scene_id``."""
    return [it for it in items if it.get(key) == scene_id]


def candidate_ids_from_raw_event(msg: Dict[str, Any]) -> List[str]:
    """Extract the candidate_ids list carried by a ``spill.candidates.raw`` event."""
    data = msg.get("data", msg) if isinstance(msg, dict) else {}
    raw = data.get("candidate_ids")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(c) for c in raw]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def check_synthetic_provenance(candidate: Dict[str, Any], expected: bool = True) -> bool:
    """Verify the candidate's ``is_synthetic`` field matches ``expected``.

    Provenance is taken from the explicit field only — never inferred from
    scene_id, filenames, or other metadata.
    """
    return _as_bool(candidate.get("is_synthetic")) is expected


def verify_provenance_chain(
    boundaries: Dict[str, Dict[str, Any]],
    expected_synthetic: bool = True,
) -> Dict[str, bool]:
    """Check is_synthetic across multiple pipeline boundaries.

    ``boundaries`` maps a boundary name (e.g. "sar.clean", "spill_candidates",
    "spill.candidates.filtered", "incident.fused") to a candidate dict that
    carries ``is_synthetic``. Returns {boundary: ok}.
    """
    report: Dict[str, bool] = {}
    for name, candidate in boundaries.items():
        if candidate is None:
            report[name] = False
        else:
            report[name] = check_synthetic_provenance(candidate, expected_synthetic)
    return report


def timeout_guard(start: float, timeout: float, stage: str) -> None:
    """Raise TimeoutError if ``stage`` exceeded ``timeout`` seconds."""
    if timeout and (time.time() - start) > timeout:
        raise TimeoutError(f"Timed out after {timeout}s at stage: {stage}")


# ── Live client (lazy heavy imports; only used inside the container) ───────────

class SARDemoError(RuntimeError):
    """Raised when a pipeline stage fails or times out."""


class SARDemoClient:
    """Talks to the real running services from inside the data-ingestion container."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, verbose: bool = False):
        self.timeout = timeout
        self.verbose = verbose
        self._redis = None
        self._pool = None
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")

    def _run(self, coro):
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Should not happen from the sync CLI; fall back to a new loop.
            return asyncio.run(coro)
        return asyncio.run(coro)

    # -- acquisition trigger (existing mechanism) -----------------------------
    def trigger_acquisition(self, inject_synthetic: bool, start: str, end: str) -> None:
        try:
            import sar_acquisition  # existing module inside the container
        except Exception as exc:  # pragma: no cover - environment dependent
            raise SARDemoError(f"trigger: cannot import sar_acquisition ({exc})")
        try:
            sar_acquisition.run_sar_acquisition(
                start, end, inject_synthetic=inject_synthetic
            )
        except Exception as exc:
            raise SARDemoError(f"trigger: SAR acquisition failed ({exc})")

    # -- Redis access ---------------------------------------------------------
    async def _get_redis(self):
        if self._redis is None:
            from shared.redis_client import get_redis
            self._redis = await get_redis()
        return self._redis

    async def read_latest_sar_clean(self, start: float) -> Dict[str, Any]:
        """Read the most recent ``sar.clean`` entry and return its parsed metadata."""
        redis = await self._get_redis()
        for _ in range(max(1, int(self.timeout / 5))):
            timeout_guard(start, self.timeout, "sar.clean publish")
            entries = await redis.xrevrange("sar.clean", "+", "-", count=1)
            if entries:
                _id, data = entries[0]
                return parse_sar_clean_message({"id": _id, "data": dict(data)})
            await self._sleep(5)
        raise SARDemoError("sar.clean: scene was never published")

    async def wait_for_filtered(self, scene_id: str, start: float) -> List[Dict[str, Any]]:
        redis = await self._get_redis()
        seen = set()
        out: List[Dict[str, Any]] = []
        while True:
            timeout_guard(start, self.timeout, "spill.candidates.filtered")
            entries = await redis.xrevrange("spill.candidates.filtered", "+", "-", count=100)
            for _id, data in entries:
                if _id in seen:
                    continue
                seen.add(_id)
                rec = {"id": _id, "data": dict(data)}
                if rec["data"].get("scene_id") == scene_id:
                    out.append(rec["data"])
            if out:
                return out
            await self._sleep(5)

    async def wait_for_fused(self, scene_id: str, start: float) -> List[Dict[str, Any]]:
        redis = await self._get_redis()
        seen = set()
        out: List[Dict[str, Any]] = []
        while True:
            timeout_guard(start, self.timeout, "incident.fused")
            entries = await redis.xrevrange("incident.fused", "+", "-", count=100)
            for _id, data in entries:
                if _id in seen:
                    continue
                seen.add(_id)
                rec = {"id": _id, "data": dict(data)}
                if rec["data"].get("scene_id") == scene_id:
                    out.append(rec["data"])
            if out:
                return out
            await self._sleep(5)

    # -- Postgres access ------------------------------------------------------
    async def wait_for_candidates(self, scene_id: str, start: float) -> List[Dict[str, Any]]:
        from shared.db.connection import create_pool, close_pool
        if self._pool is None:
            self._pool = await create_pool()
        pool = self._pool
        while True:
            timeout_guard(start, self.timeout, "spill_candidates persistence")
            rows = await pool.fetch(
                """
                SELECT candidate_id, scene_id, acquisition_time, confidence,
                       classification_label, area_m2, is_synthetic
                FROM spill_candidates
                WHERE scene_id = $1
                ORDER BY created_at ASC
                """,
                scene_id,
            )
            if rows:
                return [dict(r) for r in rows]
            await self._sleep(5)

    async def _sleep(self, secs: float) -> None:
        import asyncio
        await asyncio.sleep(secs)

    async def close(self) -> None:
        if self._pool is not None:
            from shared.db.connection import close_pool
            await close_pool()
            self._pool = None


# ── Reporting ────────────────────────────────────────────────────────────────

def _print_candidates(title: str, candidates: List[Dict[str, Any]]) -> None:
    if not candidates:
        print("  (no candidates)")
        return
    for c in candidates:
        syn = "SYNTHETIC" if _as_bool(c.get("is_synthetic")) else "real"
        print(
            f"  - candidate_id={c.get('candidate_id')} "
            f"confidence={c.get('confidence')} "
            f"label={c.get('classification_label')} "
            f"is_synthetic={syn} "
            f"area_m2={c.get('area_m2')} "
            f"acq={c.get('acquisition_time')}"
        )


def run_scenario(client: SARDemoClient, label: str, inject_synthetic: bool,
                 start: str, end: str) -> int:
    print(f"\n========== {label} ==========")
    print(f"[{label}] acquisition triggered (inject_synthetic={inject_synthetic})")
    t0 = time.time()
    client.trigger_acquisition(inject_synthetic, start, end)

    print(f"[{label}] waiting for SAR processing (sar.clean publish)...")
    meta = client._run(client.read_latest_sar_clean(t0))
    scene_id = meta.get("scene_id")
    if not scene_id:
        raise SARDemoError(f"{label}: sar.clean published but scene_id missing")
    print(f"[{label}] scene_id={scene_id} acquisition_time={meta.get('acquisition_time')} "
          f"is_synthetic(from metadata)={meta.get('is_synthetic')}")

    print(f"[{label}] waiting for lookalike/scoring (spill_candidates persistence)...")
    candidates = client._run(client.wait_for_candidates(scene_id, t0))
    print(f"[{label}] candidate count: {len(candidates)}")
    if not candidates:
        print("  No candidates produced for this run.")
    _print_candidates(label, candidates)

    print(f"[{label}] waiting for spill.candidates.filtered...")
    filtered = client._run(client.wait_for_filtered(scene_id, t0))
    print(f"[{label}] filtered events: {len(filtered)}")
    for f in filtered:
        print(f"  - candidate_id={f.get('candidate_id')} "
              f"confidence={f.get('confidence')} label={f.get('classification_label')} "
              f"is_synthetic={f.get('is_synthetic')}")

    print(f"[{label}] waiting for evidence fusion (incident.fused)...")
    fused = client._run(client.wait_for_fused(scene_id, t0))
    print(f"[{label}] incident.fused events: {len(fused)}")
    for fu in fused:
        cv = fu.get("correlated_vessel_id")
        cv = "(none)" if cv in (None, "", "null") else cv
        print(f"  - candidate_id={fu.get('candidate_id')} "
              f"confidence={fu.get('confidence')} label={fu.get('classification_label')} "
              f"is_synthetic={fu.get('is_synthetic')} correlated_vessel_id={cv}")

    # Provenance verification (Run B must be synthetic everywhere it appears).
    if candidates or filtered or fused:
        boundaries: Dict[str, Dict[str, Any]] = {}
        if meta.get("scene_id"):
            boundaries["data-ingestion/sar.clean"] = meta
        if candidates:
            boundaries["PostGIS/spill_candidates"] = candidates[0]
        if filtered:
            boundaries["spill.candidates.filtered"] = filtered[0]
        if fused:
            boundaries["incident.fused"] = fused[0]
        report = verify_provenance_chain(boundaries, expected_synthetic=inject_synthetic)
        print(f"[{label}] provenance (is_synthetic=={inject_synthetic}):")
        for name, ok in report.items():
            print(f"  - {name}: {'OK' if ok else 'MISMATCH'}")
        if inject_synthetic and not all(report.values()):
            raise SARDemoError(
                f"{label}: is_synthetic was lost at one or more boundaries: {report}"
            )
        if inject_synthetic and not fused:
            raise SARDemoError(
                f"{label}: no synthetic candidate reached incident.fused — "
                f"pipeline dropped the candidate before evidence fusion"
            )
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="End-to-end SAR spill demonstration (real + synthetic runs)."
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="Total pipeline timeout in seconds (default 1800).")
    p.add_argument("--start", type=str, default=None,
                   help="Start date YYYY-MM-DD (default: 14 days ago, UTC).")
    p.add_argument("--end", type=str, default=None,
                   help="End date YYYY-MM-DD (default: now, UTC).")
    p.add_argument("--run-a", action="store_true", help="Run A (real imagery).")
    p.add_argument("--run-b", action="store_true", help="Run B (synthetic).")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Default: run both if neither flag is given.
    run_a = args.run_a or not (args.run_a or args.run_b)
    run_b = args.run_b or not (args.run_a or args.run_b)

    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = args.start or (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    print("Aqua-Sentinel SAR Spill Demonstration")
    print(f"Date range: {start} .. {end}")
    print("Docker network: aqua-net (actual: <compose-project>_aqua-net)")
    print("\nNOTE: Confirmed oil-spill events are not guaranteed during our "
          "demonstration window, so we validate the detector using controlled "
          "synthetic perturbations while separately showing real-imagery behavior.")

    client = SARDemoClient(timeout=args.timeout, verbose=args.verbose)
    try:
        if run_a:
            run_scenario(client, "RUN A — REAL SENTINEL-1 IMAGERY", False, start, end)
        if run_b:
            run_scenario(client, "RUN B — CONTROLLED SYNTHETIC VALIDATION", True, start, end)
    except SARDemoError as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        client._run(client.close())

    print("\nDemonstration complete.")
    print("Run A demonstrates behavior on real imagery.")
    print("Run B demonstrates controlled pipeline validation; a Run B "
          "classification such as possible_oil_spill is NOT evidence of a real "
          "oil spill and is never described as 'confirmed'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
