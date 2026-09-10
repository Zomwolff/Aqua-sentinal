"""Backfill measured evidence for legacy SAR candidates with retained artifacts."""
import asyncio
import json

from shared.db.connection import create_pool
from app.scoring import score_candidate
from app.shape_filters import compute_edge_descriptors, compute_shape_descriptors
from app.texture import compute_glcm_features
from app.worker import GLCM_LEVELS, _artifact_root, _context_evidence, _resolve_candidate


async def backfill() -> tuple[int, int]:
    pool = await create_pool()
    updated = skipped = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT candidate_id, scene_id, acquisition_time, status,
                          classification_label, pixel_count, area_m2,
                          texture_features, ST_AsGeoJSON(geom) AS geojson
                   FROM spill_candidates
                   WHERE scene_id NOT LIKE 'FUSION_%'
                     AND NOT (COALESCE(texture_features, '{}'::jsonb) ? 'shape')
                   ORDER BY acquisition_time, candidate_id"""
            )
            for record in rows:
                row = dict(record)
                try:
                    resolved = await _resolve_candidate(row, _artifact_root())
                    if resolved["raw_image"] is None:
                        raise FileNotFoundError("raw SAR artifact is unavailable")
                    candidate = {
                        "candidate_id": str(row["candidate_id"]),
                        "geometry": resolved["geometry"],
                        "pixel_count": int(row["pixel_count"]),
                        "area_m2": float(row["area_m2"]) if row["area_m2"] is not None else None,
                        "classification_label": row["status"],
                    }
                    shape = compute_shape_descriptors(candidate, resolved["dark_mask"])
                    edge = compute_edge_descriptors(resolved["raw_image"], resolved["dark_mask"])
                    context = await _context_evidence(conn, row)
                    candidate["shape_features"] = shape
                    texture = compute_glcm_features(
                        resolved["raw_image"], resolved["dark_mask"], levels=GLCM_LEVELS
                    )
                    scored = score_candidate(candidate, texture, context_score=context["score"])
                    existing = row["texture_features"] or {}
                    if isinstance(existing, str):
                        existing = json.loads(existing)
                    evidence = {**existing, **texture,
                                "score_components": scored["score_components"],
                                "shape": shape, "edge": edge, "context": context,
                                "model_version": "sar-lookalike-heuristic-v1"}
                    await conn.execute(
                        """UPDATE spill_candidates
                           SET confidence=$2, texture_features=$3::jsonb
                           WHERE candidate_id=$1""",
                        row["candidate_id"], scored["confidence"], json.dumps(evidence),
                    )
                    await conn.execute(
                        """UPDATE spill_incidents SET confidence=$2
                           WHERE candidate_id=$1""",
                        row["candidate_id"], scored["confidence"],
                    )
                    updated += 1
                except (FileNotFoundError, ValueError):
                    skipped += 1
    finally:
        await pool.close()
    return updated, skipped


if __name__ == "__main__":
    print(asyncio.run(backfill()))
