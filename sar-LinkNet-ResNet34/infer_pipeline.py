"""Production-style inference pipeline for the LinkNet+ResNet34 oil-spill model.

Takes real Sentinel-1 imagery (single file or directory of tiles), runs the
trained checkpoint, and writes demo-presentable outputs per image:
``{name}_mask.png``, ``{name}_overlay.png``, ``{name}_stats.json``
(plus ``{name}_comparison.png`` only when ``--cfar-mask`` is supplied).

No accuracy numbers are computed or claimed for inputs without ground truth.
Metrics (IoU/Dice/precision/recall/F1) are reported ONLY when ``--gt-mask``
is explicitly supplied (e.g. the SOS-validation smoke test).

Usage:
    python infer_pipeline.py --checkpoint checkpoints/best_model.pth \
        --input path/to/mumbai_tiles_or_image --output-dir outputs/mumbai_demo \
        --threshold 0.5 --batch-size 4 --rescale \
        --cfar-mask path/to/cfar_result.png   # optional

Importable entry point:
    from infer_pipeline import run_inference
    results = run_inference(checkpoint=..., input=..., output_dir=...)
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from metrics import BinarySegmentationMetrics
from model import build_model
from sar_dataset import IMAGENET_MEAN, IMAGENET_STD

TILE_SIZE = 256
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

_MEAN = np.asarray(IMAGENET_MEAN, dtype=np.float32)
_STD = np.asarray(IMAGENET_STD, dtype=np.float32)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def load_model(checkpoint: str | Path, device: torch.device):
    """Load build_model() architecture + weights, set eval mode."""
    checkpoint = Path(checkpoint)
    if not checkpoint.exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint}")
    ckpt = torch.load(checkpoint, map_location=device, weights_only=False)
    state = ckpt["model_state_dict"] if isinstance(ckpt, dict) and "model_state_dict" in ckpt else ckpt
    model = build_model().to(device)
    model.load_state_dict(state)
    model.eval()
    return model


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def load_image_as_array(path: Path, rescale: bool, notes: list) -> np.ndarray:
    """Load an image file to an (H, W, 3) uint8 array, training-consistent.

    Channel handling (documented assumption — biggest domain-shift risk vs
    SOS training data, whose tiles are R=G=B grayscale-in-RGB uint8):
      - Single-band (L / I;16 / F / float GeoTIFF): replicate band to R=G=B.
      - 3-channel with R==G==B: already training-consistent, kept as-is.
      - 3-channel with R!=G!=B (e.g. false-colour composite): collapsed to a
        single channel via plain channel-mean, then replicated to R=G=B so
        the 3-channel encoder sees the same value per channel as in training.
      - RGBA: alpha dropped first, then the rule above.
      - >3 bands: plain mean across all bands, then replicated to R=G=B.

    Rescaling: if --rescale is given, min-max normalize the raw values to
    0-255 first (needed for raw dB-scale GeoTIFFs); the min/max used are
    appended to `notes` and printed so they can be sanity-checked.
    """
    with Image.open(path) as img:
        img.load()  # decouple from file handle (multi-frame TIFFs: first frame)
        raw = np.array(img)

    if raw.ndim == 2:  # (H, W) single band
        if rescale or raw.dtype != np.uint8:
            vmin, vmax = float(raw.min()), float(raw.max())
            notes.append(f"rescale single-band {raw.dtype} min={vmin:.4g} max={vmax:.4g} -> 0-255")
            print(f"  [rescale] {path.name}: raw min={vmin:.4g} max={vmax:.4g} min-max normalized to 0-255")
            raw = ((raw.astype(np.float64) - vmin) / (vmax - vmin + 1e-12) * 255.0)
        gray = raw.astype(np.float32) if raw.dtype == np.uint8 else raw.astype(np.float32).clip(0, 255)
        notes.append("single-band input replicated to R=G=B (training-consistent)")
        return np.stack([gray, gray, gray], axis=-1).clip(0, 255).astype(np.uint8)

    if raw.ndim != 3:
        raise ValueError(f"{path.name}: unsupported array shape {raw.shape}")

    h, w, c = raw.shape
    if c == 4:
        notes.append("RGBA input: alpha channel dropped")
        raw = raw[:, :, :3]
        c = 3
    if c not in (3,) and not (c > 3 or c == 2):
        raise ValueError(f"{path.name}: unsupported channel count {c}")

    if c != 3:  # 2-band or hyperspectral: mean across bands
        if rescale:
            vmin, vmax = float(raw.min()), float(raw.max())
            notes.append(f"rescale {c}-band {raw.dtype} min={vmin:.4g} max={vmax:.4g} -> 0-255")
            print(f"  [rescale] {path.name}: raw min={vmin:.4g} max={vmax:.4g} min-max normalized to 0-255")
            raw = ((raw.astype(np.float64) - vmin) / (vmax - vmin + 1e-12) * 255.0).astype(np.float32)
        gray = raw.astype(np.float32).mean(axis=2)
        notes.append(f"{c}-band input collapsed by band-mean then replicated to R=G=B "
                     "(DOMAIN-SHIFT RISK: training saw single-polarization grayscale; flag this)")
        print(f"  [channels] {path.name}: {c} bands -> band-mean replicated to R=G=B (see domain-shift note)")
        gray = gray.clip(0, 255).astype(np.uint8)
        return np.stack([gray, gray, gray], axis=-1)

    # 3-channel case.
    if raw.dtype != np.uint8 or rescale:
        vmin, vmax = float(raw.min()), float(raw.max())
        if rescale:
            notes.append(f"rescale 3-channel {raw.dtype} min={vmin:.4g} max={vmax:.4g} -> 0-255")
            print(f"  [rescale] {path.name}: raw min={vmin:.4g} max={vmax:.4g} min-max normalized to 0-255")
            raw = ((raw.astype(np.float64) - vmin) / (vmax - vmin + 1e-12) * 255.0).astype(np.uint8)
        else:
            raw = raw.astype(np.uint8)

    r, g, b = raw[:, :, 0], raw[:, :, 1], raw[:, :, 2]
    if np.array_equal(r, g) and np.array_equal(g, b):
        notes.append("R=G=B already (training-consistent grayscale-in-RGB), kept as-is")
        return raw
    gray = raw.astype(np.float32).mean(axis=2)  # plain channel-mean grayscale
    notes.append("RGB channels differ (R!=G!=B): collapsed by channel-mean then replicated "
                 "to R=G=B (DOMAIN-SHIFT RISK: training saw grayscale R=G=B; flag this)")
    print(f"  [channels] {path.name}: R!=G!=B -> channel-mean replicated to R=G=B (see domain-shift note)")
    gray = gray.clip(0, 255).astype(np.uint8)
    return np.stack([gray, gray, gray], axis=-1)


def extract_tiles(img: np.ndarray, tile: int = TILE_SIZE, overlap: int = 32):
    """Tile an (H, W, 3) image into tile x tile patches.

    Choice: overlapping tiles (default 32px overlap, stride 224) with
    logit-averaging at stitch time. Why: non-overlapping tiling is cheaper
    but produces visible seam artifacts on large scenes whenever a spill
    crosses a tile boundary (each side sees truncated context); 32px overlap
    costs ~1.3x patches ((256/224)^2) and smooths those seams by averaging
    both views' logits before thresholding. Pass --overlap 0 for pure
    non-overlapping tiling.
    Short right/bottom edges are covered by reflect-padding, cropped back
    after stitching.
    """
    h, w, _ = img.shape
    stride = tile - overlap
    if stride <= 0:
        raise ValueError(f"--overlap ({overlap}) must be < tile size ({tile})")
    if h <= tile and w <= tile and overlap == 0:
        pad_h, pad_w = tile - h, tile - w
        padded = np.pad(img, ((0, max(pad_h, 0)), (0, max(pad_w, 0)), (0, 0)), mode="reflect")
        return [(padded[:tile, :tile], 0, 0)], (h, w), padded.shape[:2]
    n_y = 1 if h <= tile else int(np.ceil((h - tile) / stride)) + 1
    n_x = 1 if w <= tile else int(np.ceil((w - tile) / stride)) + 1
    pad_h = max(0, (n_y - 1) * stride + tile - h)
    pad_w = max(0, (n_x - 1) * stride + tile - w)
    padded = np.pad(img, ((0, pad_h), (0, pad_w), (0, 0)), mode="reflect") if (pad_h or pad_w) else img
    patches = []
    for iy in range(n_y):
        for ix in range(n_x):
            y, x = iy * stride, ix * stride
            patches.append((padded[y:y + tile, x:x + tile], y, x))
    return patches, (h, w), padded.shape[:2]


def normalize_patch(patch: np.ndarray) -> torch.Tensor:
    """uint8 (256,256,3) -> normalized (3,256,256) float tensor (train same)."""
    arr = patch.astype(np.float32) / 255.0
    arr = (arr - _MEAN) / _STD
    return torch.from_numpy(arr.transpose(2, 0, 1))


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------

@torch.no_grad()
def predict_mask_for_image(img: np.ndarray, model, device: torch.device,
                           batch_size: int, overlap: int) -> np.ndarray:
    """Run tiled inference; return full-size float32 logit map (H, W)."""
    patches, (h, w), (ph, pw) = extract_tiles(img, overlap=overlap)
    tensors = torch.stack([normalize_patch(p) for p, _, _ in patches])
    logits_all = []
    for i in range(0, len(tensors), batch_size):
        batch = tensors[i:i + batch_size].to(device, non_blocking=True)
        logits_all.append(torch.sigmoid(model(batch)).cpu())  # keep probs; logit-avg ~= prob-avg here
    probs = torch.cat(logits_all, dim=0)[:, 0].numpy()  # (N, 256, 256)
    # Stitch by averaging overlapping regions (average-then-threshold).
    acc = np.zeros((ph, pw), dtype=np.float64)
    cnt = np.zeros((ph, pw), dtype=np.float64)
    for prob, y, x in zip(probs, [p[1] for p in patches], [p[2] for p in patches]):
        acc[y:y + TILE_SIZE, x:x + TILE_SIZE] += prob
        cnt[y:y + TILE_SIZE, x:x + TILE_SIZE] += 1
    return (acc / np.maximum(cnt, 1))[:h, :w].astype(np.float32)


def make_overlay(img: np.ndarray, mask: np.ndarray,
                 color: tuple = (255, 0, 0), alpha: float = 0.4) -> np.ndarray:
    """Original image with predicted oil region overlaid in semi-transparent color."""
    overlay = img.astype(np.float32).copy()
    tint = np.zeros_like(overlay)
    tint[:, :] = color
    m = mask.astype(bool)
    overlay[m] = (1 - alpha) * overlay[m] + alpha * tint[m]
    return overlay.clip(0, 255).astype(np.uint8)


def make_comparison(img: np.ndarray, cfar: np.ndarray, dl_mask: np.ndarray) -> np.ndarray:
    """Side-by-side panel: original | CFAR mask | DL mask (grayscale panels)."""
    def to_gray(a: np.ndarray) -> np.ndarray:
        if a.ndim == 3:
            a = a.mean(axis=2)
        a = a.astype(np.float32)
        if a.max() <= 1.0:
            a = a * 255.0
        return a.clip(0, 255).astype(np.uint8)
    panels = [to_gray(img)]
    for m, title in ((cfar, "cfar"), (dl_mask, "dl")):
        g = to_gray(m)
        panels.append(((g > 127).astype(np.uint8)) * 255)
    h = max(p.shape[0] for p in panels)
    padded = []
    for p in panels:
        if p.shape[0] < h:
            p = np.pad(p, ((0, h - p.shape[0]), (0, 0)), mode="constant")
        padded.append(np.stack([p, p, p], axis=-1))
    return np.concatenate(padded, axis=1)


def load_aux_mask(path: Path, shape) -> np.ndarray:
    """Load a --gt-mask / --cfar-mask file, resized only if shape mismatches (noted)."""
    with Image.open(path) as img:
        img.load()
        m = np.array(img)
    if m.ndim == 3:
        m = m.mean(axis=2)
    m = (m > 127).astype(np.uint8) * 255
    if m.shape != shape:
        print(f"  [aux] {path.name}: shape {m.shape} != image {shape}, nearest-resized to match")
        m = np.array(Image.fromarray(m).resize((shape[1], shape[0]), Image.NEAREST))
    return m


# --------------------------------------------------------------------------
# Pipeline entry point (importable)
# --------------------------------------------------------------------------

def collect_inputs(input_path: Path) -> list[Path]:
    input_path = Path(input_path)
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        paths = sorted(p for p in input_path.iterdir()
                       if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
        if not paths:
            raise RuntimeError(f"No supported images {sorted(SUPPORTED_SUFFIXES)} in {input_path}")
        return paths
    raise FileNotFoundError(f"--input not found: {input_path}")


def run_inference(checkpoint="checkpoints/best_model.pth", input=".",
                  output_dir="outputs/mumbai_demo", threshold: float = 0.5,
                  batch_size: int = 4, rescale: bool = False,
                  cfar_mask=None, gt_mask=None, overlap: int = 32,
                  device=None) -> list[dict]:
    """Run the full pipeline; returns one result dict per image.

    Metrics are computed ONLY if gt_mask is supplied, else skipped entirely.
    """
    device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    model = load_model(checkpoint, device)
    print(f"Loaded checkpoint: {checkpoint}")

    paths = collect_inputs(Path(input))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve aux masks: single file (single-image run) or directory matched by stem.
    def resolve_aux(aux, stem: str):
        if aux is None:
            return None
        aux = Path(aux)
        if aux.is_file():
            return aux
        cand = aux / f"{stem}.png"
        if cand.exists():
            return cand
        matches = sorted(aux.glob(f"{stem}.*"))
        return matches[0] if matches else None

    results = []
    with torch.no_grad():
        for path in paths:
            notes: list[str] = []
            t0 = time.perf_counter()
            raw_shape, raw_dtype = None, None
            with Image.open(path) as _probe:
                _probe.load()
                _a = np.array(_probe)
                raw_shape, raw_dtype = _a.shape, str(_a.dtype)

            img = load_image_as_array(path, rescale=rescale, notes=notes)
            h, w, _ = img.shape
            n_patches, _, _ = extract_tiles(img, overlap=overlap)
            if h == TILE_SIZE and w == TILE_SIZE and overlap == 0:
                notes.append("input already 256x256, no tiling (single patch)")
            else:
                stride = TILE_SIZE - overlap
                notes.append(f"tiled {h}x{w} -> {len(n_patches)} patches "
                             f"(tile=256, overlap={overlap}, stride={stride}, reflect-pad, logit-average stitch)")
            print(f"[preprocess] {path.name}: raw{raw_shape}/{raw_dtype} -> {h}x{w}x3 uint8 | " + "; ".join(notes))

            prob = predict_mask_for_image(img, model, device, batch_size, overlap)
            mask = (prob >= threshold)
            oil_count = int(mask.sum())
            oil_frac = float(mask.mean())
            infer_s = time.perf_counter() - t0

            stem = path.stem
            Image.fromarray(mask.astype(np.uint8) * 255).save(out_dir / f"{stem}_mask.png")
            Image.fromarray(make_overlay(img, mask)).save(out_dir / f"{stem}_overlay.png")

            stats = {
                "image": path.name,
                "input_shape": [h, w],
                "raw_shape": list(raw_shape) if raw_shape is not None else None,
                "raw_dtype": raw_dtype,
                "preprocessing": notes,
                "rescale_applied": bool(rescale),
                "tiling": {"tile": TILE_SIZE, "overlap": overlap, "num_patches": len(n_patches)},
                "oil_pixel_count": oil_count,
                "oil_pixel_fraction": oil_frac,
                "threshold": threshold,
                "inference_time_sec": round(infer_s, 3),
            }

            # Optional ground-truth metrics — ONLY when explicitly supplied.
            gt_path = resolve_aux(gt_mask, stem)
            if gt_path is not None and Path(gt_path).exists():
                gt = load_aux_mask(Path(gt_path), (h, w)) > 127
                m = BinarySegmentationMetrics(threshold=0.5)
                m.update(torch.from_numpy(prob).unsqueeze(0).unsqueeze(0),
                         torch.from_numpy(gt.astype(np.float32)).unsqueeze(0).unsqueeze(0))
                stats["gt_mask"] = str(gt_path)
                stats["metrics_vs_gt"] = m.compute()
                print(f"  [metrics] vs {Path(gt_path).name}: " +
                      ", ".join(f"{k}={v:.4f}" for k, v in stats["metrics_vs_gt"].items()))
            # else: deliberately no accuracy numbers (no ground truth).

            # Optional CFAR comparison panel.
            cfar_path = resolve_aux(cfar_mask, stem)
            if cfar_path is not None and Path(cfar_path).exists():
                cfar = load_aux_mask(Path(cfar_path), (h, w))
                Image.fromarray(make_comparison(img, cfar, mask.astype(np.uint8) * 255)).save(
                    out_dir / f"{stem}_comparison.png")
                stats["cfar_mask"] = str(cfar_path)
            # else: skipped silently (correct behaviour when not passed).

            with open(out_dir / f"{stem}_stats.json", "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)

            print(f"{path.name}: oil_fraction={oil_frac:.4f} ({oil_count} px) time={infer_s:.2f}s")
            results.append(stats)
    print(f"Wrote {len(results)} image(s) to {out_dir.resolve()}")
    print("No accuracy is claimed for outputs without an explicit --gt-mask.")
    return results


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Oil-spill inference: Sentinel-1 image(s) -> mask/overlay/stats. "
                                            "No accuracy is computed without --gt-mask.")
    p.add_argument("--checkpoint", default="checkpoints/best_model.pth")
    p.add_argument("--input", required=True, help="Image file or directory of tiles")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--rescale", action="store_true",
                   help="Min-max normalize raw values to 0-255 first (for dB-scale GeoTIFFs); prints min/max")
    p.add_argument("--overlap", type=int, default=32,
                   help="Tile overlap in px (default 32, averaged at stitch; 0 = non-overlapping)")
    p.add_argument("--cfar-mask", default=None, help="Optional CFAR mask file (or dir matched by stem)")
    p.add_argument("--gt-mask", default=None, help="Optional ground-truth mask file (or dir); "
                   "metrics computed ONLY when supplied")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_inference(checkpoint=args.checkpoint, input=args.input, output_dir=args.output_dir,
                  threshold=args.threshold, batch_size=args.batch_size, rescale=args.rescale,
                  cfar_mask=args.cfar_mask, gt_mask=args.gt_mask, overlap=args.overlap)


if __name__ == "__main__":
    main()
