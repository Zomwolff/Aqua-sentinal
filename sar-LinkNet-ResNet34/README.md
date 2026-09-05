# Running the Oil-Spill Model Guide

You received 4 Python files + 1 model file. This guide gets you from zero to oil-spill predictions. No training, no dataset, no GPU needed (GPU just makes it faster).

## 1. What you should have

Put everything like this (all files in one folder):

```
sar_model/
├── infer_pipeline.py
├── model.py
├── sar_dataset.py
├── metrics.py
├── requirements.txt
├── checkpoints/
│   └── best_model.pth   (~83 MB — must be here, or see step 4)
└── your_images/         (you create this — put Mumbai tiles/images here)
```

Missing `best_model.pth`? Ask the sender — the model cannot run without it, and it's too big for email.

## 2. Install Python + packages (one time)

1. Install **Python 3.14** from python.org (tick "Add python.exe to PATH" during install). The pinned packages target 3.14 — on older Python, skip the file and run `pip install torch segmentation-models-pytorch albumentations pillow numpy` instead.
2. Open PowerShell/terminal **in the `sar_model` folder** and run:

```powershell
pip install -r requirements.txt
```

This downloads ~2 GB (mostly PyTorch). On a slow connection, grab a coffee. CPU-only machines work fine — inference is just slower (~seconds per tile instead of instant).

## 3. Run it

Single image:

```powershell
python infer_pipeline.py --checkpoint checkpoints/best_model.pth --input your_images/tile.png --output-dir outputs/demo
```

Whole folder of tiles:

```powershell
python infer_pipeline.py --checkpoint checkpoints/best_model.pth --input your_images --output-dir outputs/demo
```

If your image is a raw satellite GeoTIFF (ends in `.tif`, looks black/weird in a normal viewer), add `--rescale`:

```powershell
python infer_pipeline.py --checkpoint checkpoints/best_model.pth --input your_images/scene.tif --output-dir outputs/demo --rescale
```

## 4. Useful knobs

| What | Flag | Example |
|---|---|---|
| Model too eager / too shy | `--threshold` (default 0.5; lower = more oil detected) | `--threshold 0.35` |
| Out of memory on big scenes | `--batch-size` (default 4; lower uses less memory) | `--batch-size 1` |
| Checkpoint kept elsewhere | `--checkpoint` | `--checkpoint D:\models\best_model.pth` |
| Compare against a CFAR mask | `--cfar-mask` | `--cfar-mask cfar_result.png` |

Accepted inputs: PNG, JPG, TIFF/GeoTIFF (any size — big scenes are auto-tiled and stitched back).

## 5. Results

For each input `tile.png`, `outputs/demo/` contains:

- `tile_mask.png` — **the main output.** Black/white oil map (white = oil), same size as your image. This is the file to use as the oil layer.
- `tile_overlay.png` — your image with predicted oil tinted **red** (the demo/show-off image). Always eyeball this: if red sits on open water instead of a dark spill patch, the model is false-alarming.
- `tile_stats.json` — the numbers (open in any text editor): `oil_pixel_count` (how many white pixels), `oil_pixel_fraction` (e.g. 0.48 = 48% of the scene — the headline number), plus `threshold`, timing, and a `preprocessing` log of exactly what was done to your file. If you passed `--gt-mask`, it also holds `metrics_vs_gt` (IoU/Dice/precision/recall) — otherwise those are absent, which is correct.
- `tile_comparison.png` — only if you used `--cfar-mask`: original | CFAR mask | model mask, side by side.

The terminal also prints one line per image: oil fraction + time taken.

## 6. Two rules (important)

1. **No accuracy numbers exist for new images.** There is no ground-truth mask for Mumbai, so IoU/Dice cannot be computed — anyone quoting them is making them up. Judge quality by looking at the red overlays.
2. **If the script prints a "DOMAIN-SHIFT" channel warning**, your input isn't plain grayscale like the training data. The script handles it automatically, but treat the result as rough and try tuning `--threshold`.

## 7. Troubleshooting

- `Checkpoint not found` → the `--checkpoint` path is wrong; check the file is really at `checkpoints/best_model.pth`.
- `No supported images...` → `--input` folder has no PNG/JPG/TIF files (check for typos, or point at a single file instead).
- `ModuleNotFoundError` → re-run `pip install -r requirements.txt` in the right folder.
- Everything runs but masks are all-black/all-white → try `--threshold 0.3` (all black) or `--threshold 0.7` (all white); for `.tif` scenes also try adding/removing `--rescale`.
- Still stuck → send the sender the full terminal text plus your `*_stats.json`.
