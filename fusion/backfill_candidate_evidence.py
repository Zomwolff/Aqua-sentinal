"""Populate measured evidence for fusion candidates created before evidence storage existed."""
import asyncio
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask, geometry_window
from rasterio.warp import transform_geom

from .app import ROOT, candidate_context, connect
from .metadata import candidate_diagnostics


def source_for_run(run_id: str) -> Path | None:
    directory = ROOT / run_id / "s1"
    if not directory.is_dir():
        return None
    return next((path for path in sorted(directory.iterdir())
                 if path.suffix.lower() in (".tif", ".tiff")), None)


async def backfill() -> tuple[int, int]:
    db = await connect()
    updated = skipped = 0
    try:
        rows = await db.fetch(
            """SELECT candidate_id, scene_id, acquisition_time,
                      ST_AsGeoJSON(geom)::jsonb AS geometry, texture_features
               FROM spill_candidates
               WHERE scene_id LIKE 'FUSION_%'
                 AND NOT (COALESCE(texture_features, '{}'::jsonb) ? 'shape')
               ORDER BY acquisition_time, candidate_id"""
        )
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["scene_id"].removeprefix("FUSION_"), []).append(row)
        for run_id, candidates in grouped.items():
            source = source_for_run(run_id)
            if not source:
                skipped += len(candidates)
                continue
            with rasterio.open(source) as src:
                raw = src.read(1).astype(np.float64)
                for row in candidates:
                    geometry = row["geometry"]
                    if isinstance(geometry, str):
                        geometry = json.loads(geometry)
                    projected = transform_geom("EPSG:4326", src.crs, geometry)
                    try:
                        window = geometry_window(src, [projected])
                        row_slice, col_slice = window.toslices()
                        selected = geometry_mask(
                            [projected],
                            (int(window.height), int(window.width)),
                            src.window_transform(window), invert=True,
                        )
                        shape, texture, edge = candidate_diagnostics(
                            raw[row_slice, col_slice], selected
                        )
                    except (ValueError, rasterio.errors.WindowError):
                        skipped += 1
                        continue
                    context = await candidate_context(db, geometry, row["acquisition_time"])
                    existing = row["texture_features"] or {}
                    if isinstance(existing, str):
                        existing = json.loads(existing)
                    evidence = {**existing, **texture, "shape": shape, "edge": edge,
                                "context": context, "model_version": "onnx-fusion-v1"}
                    await db.execute(
                        "UPDATE spill_candidates SET texture_features=$2::jsonb WHERE candidate_id=$1",
                        row["candidate_id"], json.dumps(evidence),
                    )
                    updated += 1
    finally:
        await db.close()
    return updated, skipped


if __name__ == "__main__":
    print(asyncio.run(backfill()))
