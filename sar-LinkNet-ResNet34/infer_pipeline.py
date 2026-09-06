"""
Production-style inference pipeline for the LinkNet+ResNet34 oil-spill model.

Takes Sentinel-1/SAR imagery (PNG, JPG, TIFF, GeoTIFF; single file or
directory of tiles), runs the trained checkpoint, and writes:

    {name}_mask.png
    {name}_overlay.png
    {name}_stats.json

Optional:
    {name}_comparison.png   (when --cfar-mask is supplied)

Important:
- TIFF/GeoTIFF files are read using Rasterio.
- PNG/JPG/BMP files are read using Pillow.
- GeoTIFF geographic metadata is NOT used to alter the model input.
  CRS/transform are preserved for downstream geo_postprocess.py.
- No accuracy numbers are computed unless --gt-mask is explicitly supplied.

Usage:

    python infer_pipeline.py \
        --checkpoint checkpoints/best_model.pth \
        --input path/to/mumbai_scene.tif \
        --output-dir outputs/mumbai_demo \
        --threshold 0.5 \
        --batch-size 4 \
        --rescale

Optional:

    --cfar-mask path/to/cfar_result.png
    --gt-mask path/to/ground_truth.png

Importable:

    from infer_pipeline import run_inference

    results = run_inference(
        checkpoint="checkpoints/best_model.pth",
        input="your_image.tif",
        output_dir="outputs/demo"
    )
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


# Rasterio is required for TIFF/GeoTIFF support.
try:
    import rasterio
except ImportError:
    rasterio = None


TILE_SIZE = 256

SUPPORTED_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
}

_MEAN = np.asarray(IMAGENET_MEAN, dtype=np.float32)
_STD = np.asarray(IMAGENET_STD, dtype=np.float32)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def load_model(checkpoint: str | Path, device: torch.device):
    """Load build_model() architecture + weights and set eval mode."""

    checkpoint = Path(checkpoint)

    if not checkpoint.exists():
        raise FileNotFoundError(
            f"Checkpoint not found: {checkpoint}"
        )

    ckpt = torch.load(
        checkpoint,
        map_location=device,
        weights_only=False,
    )

    state = (
        ckpt["model_state_dict"]
        if isinstance(ckpt, dict) and "model_state_dict" in ckpt
        else ckpt
    )

    model = build_model().to(device)

    model.load_state_dict(state)

    model.eval()

    return model


# --------------------------------------------------------------------------
# TIFF / GeoTIFF helpers
# --------------------------------------------------------------------------

def load_tiff_as_array(path: Path) -> np.ndarray:
    """
    Load TIFF/GeoTIFF using Rasterio.

    Returns:
        Array in either:
            (H, W) for one band
            (H, W, C) for multiple bands

    Rasterio reads bands as:
        (C, H, W)

    We transpose to:
        (H, W, C)

    This function intentionally does NOT apply geographic transformations.
    CRS/transform remain attached to the original TIFF and are handled by
    geo_postprocess.py later.
    """

    if rasterio is None:
        raise RuntimeError(
            "Rasterio is required for TIFF/GeoTIFF input.\n"
            "Install it with:\n"
            "    pip install rasterio"
        )

    try:
        with rasterio.open(path) as src:

            data = src.read()

            if data.ndim != 3:
                raise ValueError(
                    f"{path.name}: unexpected Rasterio array shape {data.shape}"
                )

            # One-band TIFF -> H,W
            if src.count == 1:
                return data[0]

            # Multi-band TIFF -> H,W,C
            return np.transpose(data, (1, 2, 0))

    except Exception as e:
        raise RuntimeError(
            f"Failed to read TIFF/GeoTIFF '{path}'.\n"
            f"Rasterio error: {e}"
        ) from e


def load_regular_image_as_array(path: Path) -> np.ndarray:
    """
    Load PNG/JPG/BMP using Pillow.

    Returns:
        numpy array.
    """

    try:
        with Image.open(path) as img:
            img.load()
            return np.array(img)

    except Exception as e:
        raise RuntimeError(
            f"Failed to read image '{path}'.\n"
            f"Pillow error: {e}"
        ) from e


def load_raw_image(path: Path) -> np.ndarray:
    """
    Load image according to file type.

    TIFF/GeoTIFF:
        Rasterio

    PNG/JPG/JPEG/BMP:
        Pillow
    """

    suffix = path.suffix.lower()

    if suffix in {".tif", ".tiff"}:
        return load_tiff_as_array(path)

    return load_regular_image_as_array(path)


def get_raw_image_metadata(path: Path):
    """
    Read raw image shape/dtype without using Pillow on TIFF.

    This fixes the original TIFF bug where:
        Image.open(path)

    was used for every file type.
    """

    raw = load_raw_image(path)

    return raw.shape, str(raw.dtype)


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def load_image_as_array(
    path: Path,
    rescale: bool,
    notes: list,
) -> np.ndarray:
    """
    Load an image file to an (H, W, 3) uint8 array.

    Training assumption:
        SOS training data uses grayscale replicated into RGB.

    Channel handling:

      Single-band:
          replicate band to R=G=B.

      3-channel grayscale:
          preserve R=G=B.

      3-channel RGB:
          channel-mean -> grayscale -> replicate.

      RGBA:
          drop alpha first.

      >3 bands:
          mean across bands -> grayscale -> replicate.

    Rescaling:

      If --rescale is supplied, raw values are min-max normalized
      to 0-255 before conversion to uint8.

      This is especially important for float GeoTIFF/SAR data.
    """

    raw = load_raw_image(path)

    # ------------------------------------------------------------------
    # Single-band input
    # ------------------------------------------------------------------

    if raw.ndim == 2:

        if rescale or raw.dtype != np.uint8:

            vmin = float(np.nanmin(raw))
            vmax = float(np.nanmax(raw))

            notes.append(
                f"rescale single-band {raw.dtype} "
                f"min={vmin:.4g} max={vmax:.4g} -> 0-255"
            )

            print(
                f"  [rescale] {path.name}: "
                f"raw min={vmin:.4g} "
                f"max={vmax:.4g} "
                f"min-max normalized to 0-255"
            )

            raw_float = raw.astype(np.float64)

            raw = (
                (raw_float - vmin)
                / (vmax - vmin + 1e-12)
                * 255.0
            )

        gray = (
            raw.astype(np.float32)
            if raw.dtype == np.uint8
            else raw.astype(np.float32).clip(0, 255)
        )

        notes.append(
            "single-band input replicated to R=G=B "
            "(training-consistent)"
        )

        return np.stack(
            [gray, gray, gray],
            axis=-1
        ).clip(0, 255).astype(np.uint8)

    # ------------------------------------------------------------------
    # Reject unsupported dimensions
    # ------------------------------------------------------------------

    if raw.ndim != 3:

        raise ValueError(
            f"{path.name}: unsupported array shape {raw.shape}"
        )

    h, w, c = raw.shape

    # ------------------------------------------------------------------
    # RGBA
    # ------------------------------------------------------------------

    if c == 4:

        notes.append(
            "RGBA input: alpha channel dropped"
        )

        raw = raw[:, :, :3]

        c = 3

    # ------------------------------------------------------------------
    # 2-band / hyperspectral
    # ------------------------------------------------------------------

    if c != 3:

        if rescale:

            vmin = float(np.nanmin(raw))
            vmax = float(np.nanmax(raw))

            notes.append(
                f"rescale {c}-band {raw.dtype} "
                f"min={vmin:.4g} max={vmax:.4g} -> 0-255"
            )

            print(
                f"  [rescale] {path.name}: "
                f"raw min={vmin:.4g} "
                f"max={vmax:.4g} "
                f"min-max normalized to 0-255"
            )

            raw_float = raw.astype(np.float64)

            raw = (
                (raw_float - vmin)
                / (vmax - vmin + 1e-12)
                * 255.0
            ).astype(np.float32)

        gray = raw.astype(np.float32).mean(axis=2)

        notes.append(
            f"{c}-band input collapsed by band-mean then "
            f"replicated to R=G=B "
            f"(DOMAIN-SHIFT RISK: training saw single-polarization "
            f"grayscale; flag this)"
        )

        print(
            f"  [channels] {path.name}: "
            f"{c} bands -> band-mean replicated to R=G=B "
            f"(see domain-shift note)"
        )

        gray = gray.clip(0, 255).astype(np.uint8)

        return np.stack(
            [gray, gray, gray],
            axis=-1
        )

    # ------------------------------------------------------------------
    # 3-channel input
    # ------------------------------------------------------------------

    if raw.dtype != np.uint8 or rescale:

        vmin = float(np.nanmin(raw))
        vmax = float(np.nanmax(raw))

        if rescale:

            notes.append(
                f"rescale 3-channel {raw.dtype} "
                f"min={vmin:.4g} max={vmax:.4g} -> 0-255"
            )

            print(
                f"  [rescale] {path.name}: "
                f"raw min={vmin:.4g} "
                f"max={vmax:.4g} "
                f"min-max normalized to 0-255"
            )

            raw_float = raw.astype(np.float64)

            raw = (
                (raw_float - vmin)
                / (vmax - vmin + 1e-12)
                * 255.0
            ).astype(np.uint8)

        else:

            raw = raw.astype(np.uint8)

    r = raw[:, :, 0]
    g = raw[:, :, 1]
    b = raw[:, :, 2]

    # Already grayscale replicated into RGB.
    if (
        np.array_equal(r, g)
        and np.array_equal(g, b)
    ):

        notes.append(
            "R=G=B already "
            "(training-consistent grayscale-in-RGB), kept as-is"
        )

        return raw

    # RGB differs -> grayscale.
    gray = raw.astype(np.float32).mean(axis=2)

    notes.append(
        "RGB channels differ (R!=G!=B): "
        "collapsed by channel-mean then replicated to R=G=B "
        "(DOMAIN-SHIFT RISK: training saw grayscale R=G=B; flag this)"
    )

    print(
        f"  [channels] {path.name}: "
        f"R!=G!=B -> channel-mean replicated to R=G=B "
        f"(see domain-shift note)"
    )

    gray = gray.clip(0, 255).astype(np.uint8)

    return np.stack(
        [gray, gray, gray],
        axis=-1
    )


# --------------------------------------------------------------------------
# Tiling
# --------------------------------------------------------------------------

def extract_tiles(
    img: np.ndarray,
    tile: int = TILE_SIZE,
    overlap: int = 32,
):
    """
    Tile an (H,W,3) image into tile x tile patches.

    Default:
        tile = 256
        overlap = 32
        stride = 224

    Overlapping tiles are stitched by averaging probabilities.

    Short right/bottom edges are reflect-padded and cropped after stitching.
    """

    h, w, _ = img.shape

    stride = tile - overlap

    if stride <= 0:

        raise ValueError(
            f"--overlap ({overlap}) must be < "
            f"tile size ({tile})"
        )

    # Simple 256x256 input with no overlap.
    if (
        h <= tile
        and w <= tile
        and overlap == 0
    ):

        pad_h = tile - h
        pad_w = tile - w

        padded = np.pad(
            img,
            (
                (0, max(pad_h, 0)),
                (0, max(pad_w, 0)),
                (0, 0),
            ),
            mode="reflect",
        )

        return (
            [(padded[:tile, :tile], 0, 0)],
            (h, w),
            padded.shape[:2],
        )

    n_y = (
        1
        if h <= tile
        else int(np.ceil((h - tile) / stride)) + 1
    )

    n_x = (
        1
        if w <= tile
        else int(np.ceil((w - tile) / stride)) + 1
    )

    pad_h = max(
        0,
        (n_y - 1) * stride + tile - h,
    )

    pad_w = max(
        0,
        (n_x - 1) * stride + tile - w,
    )

    if pad_h or pad_w:

        padded = np.pad(
            img,
            (
                (0, pad_h),
                (0, pad_w),
                (0, 0),
            ),
            mode="reflect",
        )

    else:

        padded = img

    patches = []

    for iy in range(n_y):

        for ix in range(n_x):

            y = iy * stride
            x = ix * stride

            patches.append(
                (
                    padded[
                        y:y + tile,
                        x:x + tile
                    ],
                    y,
                    x,
                )
            )

    return (
        patches,
        (h, w),
        padded.shape[:2],
    )


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def normalize_patch(
    patch: np.ndarray,
) -> torch.Tensor:
    """
    uint8 (256,256,3)
    ->
    normalized (3,256,256)
    """

    arr = patch.astype(np.float32) / 255.0

    arr = (
        arr - _MEAN
    ) / _STD

    return torch.from_numpy(
        arr.transpose(2, 0, 1)
    )


# --------------------------------------------------------------------------
# Model prediction
# --------------------------------------------------------------------------

@torch.no_grad()
def predict_mask_for_image(
    img: np.ndarray,
    model,
    device: torch.device,
    batch_size: int,
    overlap: int,
) -> np.ndarray:
    """
    Run tiled inference.

    Returns:
        Full-size float32 probability map (H,W).
    """

    patches, (h, w), (ph, pw) = extract_tiles(
        img,
        overlap=overlap,
    )

    tensors = torch.stack(
        [
            normalize_patch(p)
            for p, _, _ in patches
        ]
    )

    probs_all = []

    for i in range(
        0,
        len(tensors),
        batch_size,
    ):

        batch = tensors[
            i:i + batch_size
        ].to(
            device,
            non_blocking=True,
        )

        probs = torch.sigmoid(
            model(batch)
        ).cpu()

        probs_all.append(probs)

    probs = torch.cat(
        probs_all,
        dim=0,
    )[:, 0].numpy()

    # --------------------------------------------------------------
    # Stitch overlapping predictions.
    # --------------------------------------------------------------

    acc = np.zeros(
        (ph, pw),
        dtype=np.float64,
    )

    cnt = np.zeros(
        (ph, pw),
        dtype=np.float64,
    )

    for prob, y, x in zip(
        probs,
        [p[1] for p in patches],
        [p[2] for p in patches],
    ):

        acc[
            y:y + TILE_SIZE,
            x:x + TILE_SIZE
        ] += prob

        cnt[
            y:y + TILE_SIZE,
            x:x + TILE_SIZE
        ] += 1

    return (
        acc / np.maximum(cnt, 1)
    )[:h, :w].astype(np.float32)


# --------------------------------------------------------------------------
# Visualization
# --------------------------------------------------------------------------

def make_overlay(
    img: np.ndarray,
    mask: np.ndarray,
    color: tuple = (255, 0, 0),
    alpha: float = 0.4,
) -> np.ndarray:
    """
    Original image with predicted oil region overlaid.
    """

    overlay = img.astype(
        np.float32
    ).copy()

    tint = np.zeros_like(overlay)

    tint[:, :] = color

    m = mask.astype(bool)

    overlay[m] = (
        (1 - alpha) * overlay[m]
        + alpha * tint[m]
    )

    return overlay.clip(
        0, 255
    ).astype(np.uint8)


def make_comparison(
    img: np.ndarray,
    cfar: np.ndarray,
    dl_mask: np.ndarray,
) -> np.ndarray:
    """
    Side-by-side:

        original | CFAR | DL mask
    """

    def to_gray(a: np.ndarray) -> np.ndarray:

        if a.ndim == 3:
            a = a.mean(axis=2)

        a = a.astype(np.float32)

        if a.max() <= 1.0:
            a = a * 255.0

        return a.clip(
            0, 255
        ).astype(np.uint8)

    panels = [
        to_gray(img)
    ]

    for m, title in (
        (cfar, "cfar"),
        (dl_mask, "dl"),
    ):

        g = to_gray(m)

        panels.append(
            (
                (g > 127)
                .astype(np.uint8)
            ) * 255
        )

    h = max(
        p.shape[0]
        for p in panels
    )

    padded = []

    for p in panels:

        if p.shape[0] < h:

            p = np.pad(
                p,
                (
                    (0, h - p.shape[0]),
                    (0, 0),
                ),
                mode="constant",
            )

        padded.append(
            np.stack(
                [p, p, p],
                axis=-1,
            )
        )

    return np.concatenate(
        padded,
        axis=1,
    )


# --------------------------------------------------------------------------
# Auxiliary masks
# --------------------------------------------------------------------------

def load_aux_mask(
    path: Path,
    shape,
) -> np.ndarray:
    """
    Load --gt-mask / --cfar-mask.

    If dimensions differ, nearest-neighbor resize is used and logged.
    """

    with Image.open(path) as img:

        img.load()

        m = np.array(img)

    if m.ndim == 3:

        m = m.mean(axis=2)

    m = (
        (m > 127)
        .astype(np.uint8)
        * 255
    )

    if m.shape != shape:

        print(
            f"  [aux] {path.name}: "
            f"shape {m.shape} != image {shape}, "
            f"nearest-resized to match"
        )

        m = np.array(
            Image.fromarray(m).resize(
                (shape[1], shape[0]),
                Image.NEAREST,
            )
        )

    return m


# --------------------------------------------------------------------------
# Input collection
# --------------------------------------------------------------------------

def collect_inputs(
    input_path: Path,
) -> list[Path]:

    input_path = Path(input_path)

    if input_path.is_file():

        suffix = input_path.suffix.lower()

        if suffix not in SUPPORTED_SUFFIXES:

            raise ValueError(
                f"Unsupported input type: {suffix}\n"
                f"Supported: {sorted(SUPPORTED_SUFFIXES)}"
            )

        return [input_path]

    if input_path.is_dir():

        paths = sorted(
            p
            for p in input_path.iterdir()
            if (
                p.is_file()
                and p.suffix.lower()
                in SUPPORTED_SUFFIXES
            )
        )

        if not paths:

            raise RuntimeError(
                f"No supported images "
                f"{sorted(SUPPORTED_SUFFIXES)} "
                f"in {input_path}"
            )

        return paths

    raise FileNotFoundError(
        f"--input not found: {input_path}"
    )


# --------------------------------------------------------------------------
# Main inference pipeline
# --------------------------------------------------------------------------

def run_inference(
    checkpoint="checkpoints/best_model.pth",
    input=".",
    output_dir="outputs/mumbai_demo",
    threshold: float = 0.5,
    batch_size: int = 4,
    rescale: bool = False,
    cfar_mask=None,
    gt_mask=None,
    overlap: int = 32,
    device=None,
) -> list[dict]:
    """
    Run inference.

    Metrics are computed ONLY when gt_mask is supplied.
    """

    device = device or torch.device(
        "cuda"
        if torch.cuda.is_available()
        else "cpu"
    )

    print(f"Device: {device}")

    model = load_model(
        checkpoint,
        device,
    )

    print(
        f"Loaded checkpoint: {checkpoint}"
    )

    paths = collect_inputs(
        Path(input)
    )

    out_dir = Path(output_dir)

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------------
    # Auxiliary mask resolver
    # --------------------------------------------------------------

    def resolve_aux(
        aux,
        stem: str,
    ):

        if aux is None:
            return None

        aux = Path(aux)

        if aux.is_file():
            return aux

        candidate = (
            aux / f"{stem}.png"
        )

        if candidate.exists():
            return candidate

        matches = sorted(
            aux.glob(
                f"{stem}.*"
            )
        )

        return (
            matches[0]
            if matches
            else None
        )

    results = []

    # --------------------------------------------------------------
    # Process every image.
    # --------------------------------------------------------------

    for path in paths:

        notes: list[str] = []

        t0 = time.perf_counter()

        # ----------------------------------------------------------
        # FIX:
        #
        # Previously this used:
        #
        #     Image.open(path)
        #
        # for TIFF files too.
        #
        # Now TIFF uses Rasterio.
        # ----------------------------------------------------------

        raw_shape, raw_dtype = (
            get_raw_image_metadata(path)
        )

        # ----------------------------------------------------------
        # Load and preprocess.
        # ----------------------------------------------------------

        img = load_image_as_array(
            path,
            rescale=rescale,
            notes=notes,
        )

        h, w, _ = img.shape

        n_patches, _, _ = extract_tiles(
            img,
            overlap=overlap,
        )

        if (
            h == TILE_SIZE
            and w == TILE_SIZE
            and overlap == 0
        ):

            notes.append(
                "input already 256x256, "
                "no tiling (single patch)"
            )

        else:

            stride = (
                TILE_SIZE - overlap
            )

            notes.append(
                f"tiled {h}x{w} -> "
                f"{len(n_patches)} patches "
                f"(tile={TILE_SIZE}, "
                f"overlap={overlap}, "
                f"stride={stride}, "
                f"reflect-pad, "
                f"probability-average stitch)"
            )

        print(
            f"[preprocess] {path.name}: "
            f"raw{raw_shape}/{raw_dtype} "
            f"-> {h}x{w}x3 uint8 | "
            + "; ".join(notes)
        )

        # ----------------------------------------------------------
        # Model prediction.
        # ----------------------------------------------------------

        prob = predict_mask_for_image(
            img,
            model,
            device,
            batch_size,
            overlap,
        )

        mask = (
            prob >= threshold
        )

        oil_count = int(
            mask.sum()
        )

        oil_frac = float(
            mask.mean()
        )

        infer_s = (
            time.perf_counter()
            - t0
        )

        stem = path.stem

        # ----------------------------------------------------------
        # Save mask.
        # ----------------------------------------------------------

        mask_path = (
            out_dir
            / f"{stem}_mask.png"
        )

        Image.fromarray(
            mask.astype(np.uint8) * 255
        ).save(mask_path)

        # ----------------------------------------------------------
        # Save overlay.
        # ----------------------------------------------------------

        overlay_path = (
            out_dir
            / f"{stem}_overlay.png"
        )

        Image.fromarray(
            make_overlay(
                img,
                mask,
            )
        ).save(overlay_path)

        # ----------------------------------------------------------
        # Stats.
        # ----------------------------------------------------------

        stats = {
            "image": path.name,

            "input_shape": [
                h,
                w,
            ],

            "raw_shape": (
                list(raw_shape)
                if raw_shape is not None
                else None
            ),

            "raw_dtype": raw_dtype,

            "preprocessing": notes,

            "rescale_applied": bool(
                rescale
            ),

            "tiling": {
                "tile": TILE_SIZE,
                "overlap": overlap,
                "num_patches": len(
                    n_patches
                ),
            },

            "oil_pixel_count": oil_count,

            "oil_pixel_fraction": oil_frac,

            "threshold": threshold,

            "inference_time_sec": round(
                infer_s,
                3,
            ),
        }

        # ----------------------------------------------------------
        # Optional ground truth metrics.
        # ----------------------------------------------------------

        gt_path = resolve_aux(
            gt_mask,
            stem,
        )

        if (
            gt_path is not None
            and Path(gt_path).exists()
        ):

            gt = (
                load_aux_mask(
                    Path(gt_path),
                    (h, w),
                )
                > 127
            )

            m = BinarySegmentationMetrics(
                threshold=0.5
            )

            m.update(
                torch.from_numpy(
                    prob
                )
                .unsqueeze(0)
                .unsqueeze(0),

                torch.from_numpy(
                    gt.astype(
                        np.float32
                    )
                )
                .unsqueeze(0)
                .unsqueeze(0),
            )

            stats["gt_mask"] = str(
                gt_path
            )

            stats[
                "metrics_vs_gt"
            ] = m.compute()

            print(
                f"  [metrics] vs "
                f"{Path(gt_path).name}: "
                + ", ".join(
                    f"{k}={v:.4f}"
                    for k, v
                    in stats[
                        "metrics_vs_gt"
                    ].items()
                )
            )

        # ----------------------------------------------------------
        # Optional CFAR comparison.
        # ----------------------------------------------------------

        cfar_path = resolve_aux(
            cfar_mask,
            stem,
        )

        if (
            cfar_path is not None
            and Path(cfar_path).exists()
        ):

            cfar = load_aux_mask(
                Path(cfar_path),
                (h, w),
            )

            comparison = make_comparison(
                img,
                cfar,
                mask.astype(
                    np.uint8
                ) * 255,
            )

            Image.fromarray(
                comparison
            ).save(
                out_dir
                / f"{stem}_comparison.png"
            )

            stats["cfar_mask"] = str(
                cfar_path
            )

        # ----------------------------------------------------------
        # Save JSON.
        # ----------------------------------------------------------

        stats_path = (
            out_dir
            / f"{stem}_stats.json"
        )

        with open(
            stats_path,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                stats,
                f,
                indent=2,
            )

        print(
            f"{path.name}: "
            f"oil_fraction={oil_frac:.4f} "
            f"({oil_count} px) "
            f"time={infer_s:.2f}s"
        )

        results.append(stats)

    print(
        f"Wrote {len(results)} image(s) "
        f"to {out_dir.resolve()}"
    )

    print(
        "No accuracy is claimed for outputs "
        "without an explicit --gt-mask."
    )

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:

    p = argparse.ArgumentParser(
        description=(
            "Oil-spill inference: "
            "Sentinel-1 image(s) -> "
            "mask/overlay/stats."
        )
    )

    p.add_argument(
        "--checkpoint",
        default="checkpoints/best_model.pth",
    )

    p.add_argument(
        "--input",
        required=True,
        help="Image file or directory of tiles",
    )

    p.add_argument(
        "--output-dir",
        required=True,
    )

    p.add_argument(
        "--threshold",
        type=float,
        default=0.5,
    )

    p.add_argument(
        "--batch-size",
        type=int,
        default=4,
    )

    p.add_argument(
        "--rescale",
        action="store_true",
        help=(
            "Min-max normalize raw values "
            "to 0-255 first. "
            "Recommended for float/dB-scale "
            "GeoTIFFs."
        ),
    )

    p.add_argument(
        "--overlap",
        type=int,
        default=32,
        help=(
            "Tile overlap in pixels "
            "(default 32; "
            "0 = non-overlapping)"
        ),
    )

    p.add_argument(
        "--cfar-mask",
        default=None,
        help=(
            "Optional CFAR mask file "
            "or directory matched by stem"
        ),
    )

    p.add_argument(
        "--gt-mask",
        default=None,
        help=(
            "Optional ground-truth mask file "
            "or directory; metrics are computed "
            "ONLY when supplied"
        ),
    )

    return p.parse_args()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:

    args = parse_args()

    run_inference(
        checkpoint=args.checkpoint,
        input=args.input,
        output_dir=args.output_dir,
        threshold=args.threshold,
        batch_size=args.batch_size,
        rescale=args.rescale,
        cfar_mask=args.cfar_mask,
        gt_mask=args.gt_mask,
        overlap=args.overlap,
    )


if __name__ == "__main__":
    main()