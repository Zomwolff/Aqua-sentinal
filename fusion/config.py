from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = PROJECT_ROOT / "models"
SAR_ONNX = PROJECT_ROOT / "models" / "sar" / "model.onnx"
EO_ONNX = PROJECT_ROOT / "models" / "eo" / "model.onnx"
EO_NORMALIZATION = MODEL_ROOT / "eo" / "normalization.json"
SAR_THRESHOLD = 0.50
EO_THRESHOLD = 0.15
CANONICAL_BANDS = ("B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12")
