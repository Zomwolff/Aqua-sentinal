"""Local Sentinel uploads and handoff to evidence/attribution workers."""
import asyncio
import json
import os
import uuid
from pathlib import Path
import asyncpg
from redis.asyncio import Redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fusion.config import SAR_ONNX, EO_ONNX, SAR_THRESHOLD, EO_THRESHOLD
from fusion.metadata import run_fusion, parse_time

app = FastAPI()
ROOT = Path('/data/artifacts/fusion')

async def connect():
    return await asyncpg.connect(host=os.getenv('POSTGRES_HOST', 'postgres'),
        port=int(os.getenv('POSTGRES_PORT', '5432')), user=os.getenv('POSTGRES_USER', 'aqua_sentinel'),
        password=os.getenv('POSTGRES_PASSWORD', 'change_me'), database=os.getenv('POSTGRES_DB', 'aqua_sentinel'))

async def save(upload, directory):
    name = Path((upload.filename or 'image.tif').replace('\\', '/')).name
    if Path(name).suffix.lower() not in ('.tif', '.tiff'):
        raise ValueError('Upload a TIFF image')
    path = directory / name
    total = 0
    with path.open('wb') as target:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise ValueError('Maximum file size is 100 MB')
            target.write(chunk)
    if not total:
        raise ValueError('Empty upload')
    return path


async def candidate_context(db, geometry, acquisition_time):
    nearest = await db.fetchrow(
        """SELECT v.mmsi, v.name AS vessel_name, vp.timestamp,
                  ST_Distance(vp.geom::geography, ST_SetSRID(ST_GeomFromGeoJSON($1),4326)::geography) AS distance_m,
                  ABS(EXTRACT(EPOCH FROM (vp.timestamp-$2::timestamptz)))/3600.0 AS time_gap_hours
           FROM vessel_positions vp JOIN vessels v ON v.id=vp.vessel_id
           WHERE vp.timestamp BETWEEN $2::timestamptz-INTERVAL '6 hours' AND $2::timestamptz+INTERVAL '6 hours'
             AND ST_DWithin(vp.geom::geography, ST_SetSRID(ST_GeomFromGeoJSON($1),4326)::geography, 20000.0)
           ORDER BY vp.geom <-> ST_Centroid(ST_SetSRID(ST_GeomFromGeoJSON($1),4326)), time_gap_hours
           LIMIT 1""",
        json.dumps(geometry), acquisition_time,
    )
    if not nearest:
        return {"score": 0.0, "source": "saved_ais", "nearby_vessel_found": False}
    distance = float(nearest["distance_m"])
    gap = float(nearest["time_gap_hours"])
    return {"score": 0.6 * max(0.0, 1.0-distance/20_000.0) + 0.4 * max(0.0, 1.0-gap/6.0),
            "source": "saved_ais", "nearby_vessel_found": True,
            "nearby_vessel_mmsi": str(nearest["mmsi"]), "nearby_vessel_name": nearest["vessel_name"],
            "vessel_distance_m": distance, "time_gap_hours": gap,
            "position_timestamp": nearest["timestamp"].isoformat()}

@app.get('/health')
def health():
    ready = SAR_ONNX.is_file() and EO_ONNX.is_file()
    return {'status': 'ok' if ready else 'missing_models', 'sar_model': SAR_ONNX.is_file(),
            'eo_model': EO_ONNX.is_file(), 'sar_threshold': SAR_THRESHOLD, 'eo_threshold': EO_THRESHOLD}

@app.post('/upload')
async def upload(sentinel1: UploadFile | None = File(None), sentinel2: UploadFile | None = File(None),
                 sentinel2_bands: list[UploadFile] | None = File(None), mmsi: str | None = Form(None),
                 acquisition_time: str | None = Form(None), scene_id: str | None = Form(None)):
    if not any((sentinel1, sentinel2, sentinel2_bands)) or (sentinel2 and sentinel2_bands):
        raise HTTPException(422, 'Provide S1, S2, or both. Use either an S2 stack or separate bands.')
    run = uuid.uuid4().hex
    out = ROOT / run
    out.mkdir(parents=True)
    db = await connect()
    redis = Redis(host=os.getenv('REDIS_HOST', 'redis'), port=int(os.getenv('REDIS_PORT', '6379')))
    try:
        vessel = await db.fetchrow('''SELECT v.id, p.timestamp FROM vessels v
            LEFT JOIN LATERAL (SELECT timestamp FROM vessel_positions WHERE vessel_id=v.id
            ORDER BY timestamp DESC LIMIT 1) p ON TRUE WHERE v.mmsi=$1''', mmsi) if mmsi else None
        if mmsi and not vessel:
            raise ValueError('Selected vessel was not found')
        paths = []
        for key, file in (('s1', sentinel1), ('s2', sentinel2)):
            directory = out / key
            directory.mkdir()
            paths.append(await save(file, directory) if file else None)
        if sentinel2_bands:
            directory = out / 's2_bands'
            directory.mkdir()
            names = [Path(f.filename or '').name for f in sentinel2_bands]
            if len(set(names)) != len(names):
                raise ValueError('Duplicate S2 filenames')
            for file in sentinel2_bands:
                await save(file, directory)
            paths[1] = directory
        result = await asyncio.to_thread(run_fusion, *paths, output_dir=out,
            acquisition_time=acquisition_time, scene_id=scene_id,
            ais_time=vessel['timestamp'] if vessel else None)
        result['run_id'] = run
        result['mmsi'] = mmsi
        result['artifacts'] = {k: f'/artifacts/fusion/{run}/{v}' for k, v in result['artifacts'].items()}
        result['pipeline_scene_id'] = 'FUSION_' + run
        metadata = {k: v for k, v in result.items() if k != 'candidates'}
        async with db.transaction():
            for candidate in result['candidates']:
                candidate['context'] = await candidate_context(
                    db, candidate['geometry'], parse_time(result['acquisition_time']))
                evidence = {**candidate['texture'], 'shape': candidate['shape'],
                            'edge': candidate['edge'], 'context': candidate['context'],
                            'model_version': candidate['model_version'], 'fusion_metadata': metadata}
                await db.execute('''INSERT INTO spill_candidates
                    (candidate_id, scene_id, acquisition_time, geom, area_m2, pixel_count,
                     status, classification_label, confidence, texture_features)
                    VALUES ($1,$2,$3,ST_SetSRID(ST_GeomFromGeoJSON($4),4326),$5,$6,
                            'possible_oil_spill','possible_oil_spill',$7,$8::jsonb)''',
                    uuid.UUID(candidate['candidate_id']), result['pipeline_scene_id'], parse_time(result['acquisition_time']),
                    json.dumps(candidate['geometry']), candidate['area_m2'], candidate['pixel_count'],
                    candidate['confidence'], json.dumps(evidence))
            if vessel and sentinel1:
                await db.execute('''INSERT INTO satellite_tasking_requests
                    (vessel_id,mmsi,reason,status,scene_id,completed_at)
                    VALUES ($1,$2,$3::jsonb,'fulfilled',$4,NOW())''', vessel['id'], mmsi,
                    json.dumps({'source': 'manual_fusion', 'fusion_metadata': metadata}), result['pipeline_scene_id'])
        for candidate in result['candidates']:
            payload = {'candidate_id': candidate['candidate_id'], 'scene_id': result['pipeline_scene_id'],
                       'acquisition_time': result['acquisition_time'], 'confidence': candidate['confidence'],
                       'classification_label': 'possible_oil_spill', 'is_synthetic': False,
                       'area_m2': candidate['area_m2'], 'area_km2': candidate['area_km2'],
                       'texture_features': {**candidate['texture'], 'shape': candidate['shape'],
                           'edge': candidate['edge'], 'context': candidate['context'],
                           'model_version': candidate['model_version']}}
            await redis.xadd('spill.candidates.filtered', {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()})
        result['pipeline_status'] = 'submitted' if result['candidates'] else 'complete'
        (out / 'fusion_metadata.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await db.close()
        await redis.aclose()
