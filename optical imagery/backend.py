"""HTTP backend for the Sentinel-2 oil-spill segmentation model.

Run from this directory with:
    python -m uvicorn backend:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent
INFERENCE_SCRIPT = BASE_DIR / "sentinel2_interface.py"
CHECKPOINT = BASE_DIR / "sentinel2_unet_binary_best.pth"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
UPLOADS_DIR = ARTIFACTS_DIR / "uploads"
RESULTS_DIR = ARTIFACTS_DIR / "results"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
INFERENCE_TIMEOUT_SECONDS = 30 * 60
ALLOWED_SUFFIXES = {".tif", ".tiff"}

app = FastAPI(
    title="Sentinel-2 Oil Spill Model",
    version="1.0.0",
    description="Runs the local Sentinel-2 U-Net model on uploaded GeoTIFF imagery.",
)


def _ensure_directories() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_result_file(job_id: str, filename: str) -> Path:
    result_dir = (RESULTS_DIR / job_id).resolve()
    path = (result_dir / filename).resolve()
    if result_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Result file not found")
    return path


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if INFERENCE_SCRIPT.is_file() and CHECKPOINT.is_file() else "degraded",
        "service": "sentinel2-oil-spill-model",
        "model_available": CHECKPOINT.is_file(),
    }


@app.get("/model")
def model_info() -> dict:
    if not CHECKPOINT.is_file():
        raise HTTPException(status_code=503, detail="Model checkpoint is missing")
    return {
        "model": "BinaryUNet",
        "checkpoint": CHECKPOINT.name,
        "required_bands": ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"],
    }


@app.post("/predict")
def predict(
    image: UploadFile = File(...),
    threshold: float | None = Form(default=None),
) -> dict:
    if not INFERENCE_SCRIPT.is_file() or not CHECKPOINT.is_file():
        raise HTTPException(status_code=503, detail="Inference script or model checkpoint is missing")
    if not image.filename or Path(image.filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Upload a GeoTIFF file with a .tif or .tiff extension")
    if threshold is not None and not 0 <= threshold <= 1:
        raise HTTPException(status_code=400, detail="threshold must be between 0 and 1")

    _ensure_directories()
    job_id = uuid.uuid4().hex
    input_path = UPLOADS_DIR / f"{job_id}{Path(image.filename).suffix.lower()}"
    output_dir = RESULTS_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with input_path.open("wb") as destination:
            total_bytes = 0
            while chunk := image.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Uploaded file is too large")
                destination.write(chunk)

        command = [
            sys.executable,
            str(INFERENCE_SCRIPT),
            str(input_path),
            "--checkpoint",
            str(CHECKPOINT),
            "--output-dir",
            str(output_dir),
        ]
        if threshold is not None:
            command.extend(["--threshold", str(threshold)])

        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=INFERENCE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Inference failed"
            raise HTTPException(status_code=422, detail=detail[-4000:])

        metadata_path = output_dir / "metadata.json"
        if not metadata_path.is_file():
            raise HTTPException(status_code=500, detail="Inference completed without metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["job_id"] = job_id
        metadata["files"] = {
            path.name: f"/results/{job_id}/{path.name}"
            for path in output_dir.iterdir()
            if path.is_file()
        }
        return metadata
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Inference timed out") from exc
    finally:
        input_path.unlink(missing_ok=True)


@app.get("/results/{job_id}/{filename}")
def result_file(job_id: str, filename: str) -> FileResponse:
    path = _safe_result_file(job_id, filename)
    return FileResponse(path, filename=path.name)
