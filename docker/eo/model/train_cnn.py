# ============================================================
# MADOS SENTINEL-2 CNN
# CORRECTED 10-BAND SPATIAL-SPECTRAL VERSION
#
# FINAL / BEST VERSION
#
# Features:
#   - Same scene-level split as RF
#   - 10 Sentinel-2 bands
#   - 32x32 spatial windows
#   - Training-only normalization
#   - Full-window NaN/Inf checking
#   - Class-weighted CrossEntropy
#   - Best model selected using Validation Oil F1
#   - Training curves
#   - Oil probability analysis
#   - Calibration curve
#   - Confusion matrix
#
# MODEL OUTPUT:
#   sentinel2_cnn_10band_best.pth
# ============================================================


# ============================================================
# IMPORTS
# ============================================================

from pathlib import Path
from collections import defaultdict
import warnings
import random

import numpy as np
import rasterio
from rasterio.enums import Resampling

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    precision_recall_fscore_support,
)
from sklearn.calibration import calibration_curve

import matplotlib.pyplot as plt


# ============================================================
# CONFIG
# ============================================================

MADOS_ROOT = Path(r"D:\ranodm\MADOS\MADOS")

# ------------------------------------------------------------
# Sampling
# ------------------------------------------------------------

SAMPLES_PER_CROP = 300

# ------------------------------------------------------------
# Spatial window
# ------------------------------------------------------------

PATCH_SIZE = 32

# ------------------------------------------------------------
# Reproducibility
# ------------------------------------------------------------

RANDOM_STATE = 42

# ------------------------------------------------------------
# Scene split
# ------------------------------------------------------------

TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
TEST_RATIO = 0.15

# ------------------------------------------------------------
# Training
# ------------------------------------------------------------

BATCH_SIZE = 128
EPOCHS = 20

LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-4

NUM_WORKERS = 0

EVAL_ONLY = True
# ------------------------------------------------------------
# Model output
#
# IMPORTANT:
# This is intentionally different from the previous CNN file.
# ------------------------------------------------------------

MODEL_PATH = (
    MADOS_ROOT.parent
    / "sentinel2_cnn_10band_best.pth"
)


# ============================================================
# REPRODUCIBILITY
# ============================================================

random.seed(RANDOM_STATE)
np.random.seed(RANDOM_STATE)
torch.manual_seed(RANDOM_STATE)

if torch.cuda.is_available():
    torch.cuda.manual_seed_all(RANDOM_STATE)


# ============================================================
# DEVICE
# ============================================================

DEVICE = torch.device(
    "cuda"
    if torch.cuda.is_available()
    else "cpu"
)

print()
print("=" * 60)
print("DEVICE")
print("=" * 60)

print(f"PyTorch version: {torch.__version__}")
print(f"Device: {DEVICE}")

if torch.cuda.is_available():

    print(
        f"GPU: "
        f"{torch.cuda.get_device_name(0)}"
    )

    print(
        f"CUDA version: "
        f"{torch.version.cuda}"
    )

else:

    print("CUDA available: False")
    print("Using CPU")


# ============================================================
# CLASS NAMES
# ============================================================

CLASS_NAMES = {
    1: "Marine Debris",
    2: "Dense Sargassum",
    3: "Sparse Floating Algae",
    4: "Natural Organic Material",
    5: "Ship",
    6: "Oil Spill",
    7: "Marine Water",
    8: "Sediment-Laden Water",
    9: "Foam",
    10: "Turbid Water",
    11: "Shallow Water",
    12: "Waves & Wakes",
    13: "Oil Platform",
    14: "Jellyfish",
    15: "Sea snot",
}

CLASS_IDS = sorted(CLASS_NAMES.keys())

NUM_CLASSES = len(CLASS_IDS)

# PyTorch internally uses 0..14
# MADOS labels are 1..15.
CLASS_TO_INDEX = {
    class_id: class_id - 1
    for class_id in CLASS_IDS
}

INDEX_TO_CLASS = {
    class_id - 1: class_id
    for class_id in CLASS_IDS
}

OIL_CLASS_ID = 6
OIL_INDEX = OIL_CLASS_ID - 1


# ============================================================
# BAND DEFINITIONS
# ============================================================
#
# Same corrected band handling as RF.
#
# B2  = 492
# B3  = 559 / 560
# B4  = 665
# B5  = 704
# B6  = 739 / 740
# B7  = 780 / 783
# B8  = 833
# B8A = 864 / 865
# B11 = 1610 / 1614
# B12 = 2186 / 2202
#
# ============================================================

BAND_WAVELENGTHS = {

    "B2": [492],

    "B3": [559, 560],

    "B4": [665],

    "B5": [704],

    "B6": [739, 740],

    "B7": [780, 783],

    "B8": [833],

    "B8A": [864, 865],

    "B11": [1610, 1614],

    "B12": [2186, 2202],
}

FEATURE_NAMES = list(
    BAND_WAVELENGTHS.keys()
)

NUM_BANDS = len(FEATURE_NAMES)


# ============================================================
# FIND BAND FILE
# ============================================================

def find_band_file(
    folder,
    prefix,
    wavelengths,
    patch,
):
    """
    Find the actual MADOS band file.

    Some MADOS files use slightly different
    wavelength labels for the same Sentinel-2 band.
    """

    for wavelength in wavelengths:

        candidate = (
            folder
            / f"{prefix}_L2R_rhorc_{wavelength}_{patch}.tif"
        )

        if candidate.exists():

            return candidate

    return None


# ============================================================
# FIND ALL CROPS
# ============================================================

def get_crops(root):

    print()
    print("=" * 60)
    print("Finding MADOS patches...")
    print("=" * 60)

    # B2 is used as the reference patch list.
    b2_files = sorted(
        root.glob(
            "Scene_*/10/*_L2R_rhorc_492_*.tif"
        )
    )

    print(
        f"Found {len(b2_files)} B2 patches."
    )

    crops = []

    missing_counter = defaultdict(int)

    for b2_file in b2_files:

        scene_dir = (
            b2_file.parent.parent
        )

        scene_name = scene_dir.name

        patch = (
            b2_file.stem
            .rsplit("_", 1)[1]
        )

        prefix = (
            b2_file.name
            .split("_L2R_")[0]
        )

        folder_10 = (
            scene_dir / "10"
        )

        folder_20 = (
            scene_dir / "20"
        )

        files = {

            "scene": scene_name,

            "patch": patch,

            "B2": b2_file,
        }

        missing = []

        # ----------------------------------------------------
        # 10m bands
        # ----------------------------------------------------

        for band in [
            "B3",
            "B4",
            "B8",
        ]:

            file = find_band_file(
                folder_10,
                prefix,
                BAND_WAVELENGTHS[band],
                patch,
            )

            if file is None:

                missing.append(band)

            else:

                files[band] = file

        # ----------------------------------------------------
        # 20m bands
        # ----------------------------------------------------

        for band in [
            "B5",
            "B6",
            "B7",
            "B8A",
            "B11",
            "B12",
        ]:

            file = find_band_file(
                folder_20,
                prefix,
                BAND_WAVELENGTHS[band],
                patch,
            )

            if file is None:

                missing.append(band)

            else:

                files[band] = file

        # ----------------------------------------------------
        # Labels
        # ----------------------------------------------------

        label_file = (
            folder_10
            / f"{prefix}_L2R_cl_{patch}.tif"
        )

        conf_file = (
            folder_10
            / f"{prefix}_L2R_conf_{patch}.tif"
        )

        if not label_file.exists():

            missing.append("label")

        else:

            files["label"] = label_file

        if not conf_file.exists():

            missing.append("confidence")

        else:

            files["confidence"] = conf_file

        # ----------------------------------------------------
        # Skip incomplete crops
        # ----------------------------------------------------

        if missing:

            missing_counter[
                tuple(missing)
            ] += 1

            continue

        crops.append(files)

    print()
    print(
        f"Usable complete patches: "
        f"{len(crops)}"
    )

    print(
        f"Skipped incomplete patches: "
        f"{sum(missing_counter.values())}"
    )

    if missing_counter:

        print()
        print("Missing-file combinations:")

        for key, count in sorted(
            missing_counter.items(),
            key=lambda x: -x[1],
        ):

            print(
                f"  {count:5d} -> "
                f"{', '.join(key)}"
            )

    return crops


# ============================================================
# LOAD FULL CROP
# ============================================================

def load_full_crop(crop):
    """
    Load a complete MADOS crop.

    Returns:

        X:
            H x W x 10

        labels:
            H x W

        confidence:
            H x W
    """

    try:

        # ----------------------------------------------------
        # B2 reference
        # ----------------------------------------------------

        with warnings.catch_warnings():

            warnings.simplefilter(
                "ignore"
            )

            with rasterio.open(
                crop["B2"]
            ) as src:

                b2 = src.read(
                    1
                ).astype(
                    np.float32
                )

        target_shape = b2.shape

        bands = {}

        # ----------------------------------------------------
        # 10m bands
        # ----------------------------------------------------

        for band in [
            "B2",
            "B3",
            "B4",
            "B8",
        ]:

            with warnings.catch_warnings():

                warnings.simplefilter(
                    "ignore"
                )

                with rasterio.open(
                    crop[band]
                ) as src:

                    data = src.read(
                        1
                    ).astype(
                        np.float32
                    )

            bands[band] = data

        # ----------------------------------------------------
        # 20m bands
        # ----------------------------------------------------

        for band in [
            "B5",
            "B6",
            "B7",
            "B8A",
            "B11",
            "B12",
        ]:

            with warnings.catch_warnings():

                warnings.simplefilter(
                    "ignore"
                )

                with rasterio.open(
                    crop[band]
                ) as src:

                    data = src.read(
                        1,
                        out_shape=target_shape,
                        resampling=Resampling.nearest,
                    ).astype(
                        np.float32
                    )

            bands[band] = data

        # ----------------------------------------------------
        # Labels
        # ----------------------------------------------------

        with warnings.catch_warnings():

            warnings.simplefilter(
                "ignore"
            )

            with rasterio.open(
                crop["label"]
            ) as src:

                labels = src.read(1)

        # ----------------------------------------------------
        # Confidence
        # ----------------------------------------------------

        with warnings.catch_warnings():

            warnings.simplefilter(
                "ignore"
            )

            with rasterio.open(
                crop["confidence"]
            ) as src:

                confidence = src.read(1)

        # ----------------------------------------------------
        # Stack bands
        # ----------------------------------------------------

        X = np.stack(
            [
                bands[name]
                for name in FEATURE_NAMES
            ],
            axis=-1,
        )

        return (
            X,
            labels.astype(np.int16),
            confidence,
        )

    except Exception as e:

        print(
            f"ERROR loading "
            f"{crop['scene']} "
            f"patch {crop['patch']}: {e}"
        )

        return None, None, None


# ============================================================
# SCENE STATISTICS
# ============================================================

def get_scene_statistics(crops):

    scene_crops = defaultdict(list)

    for crop in crops:

        scene_crops[
            crop["scene"]
        ].append(crop)

    scene_stats = {}

    print()
    print(
        "Calculating scene statistics..."
    )

    for scene, scene_crop_list in (
        scene_crops.items()
    ):

        oil_pixels = 0
        labelled_pixels = 0

        for crop in scene_crop_list:

            try:

                with warnings.catch_warnings():

                    warnings.simplefilter(
                        "ignore"
                    )

                    with rasterio.open(
                        crop["label"]
                    ) as src:

                        labels = src.read(1)

                    with rasterio.open(
                        crop["confidence"]
                    ) as src:

                        confidence = src.read(1)

                valid = (
                    (labels > 0)
                    &
                    (confidence == 1)
                )

                labelled = np.sum(
                    valid
                )

                oil = np.sum(
                    valid
                    &
                    (labels == OIL_CLASS_ID)
                )

                labelled_pixels += (
                    labelled
                )

                oil_pixels += oil

            except Exception:

                continue

        if labelled_pixels > 0:

            oil_ratio = (
                oil_pixels
                / labelled_pixels
            )

        else:

            oil_ratio = 0.0

        scene_stats[scene] = {

            "oil_pixels":
                int(oil_pixels),

            "labelled_pixels":
                int(labelled_pixels),

            "oil_ratio":
                float(oil_ratio),
        }

    return scene_stats


# ============================================================
# SCENE-LEVEL STRATIFIED SPLIT
# ============================================================

def split_by_scene(crops):

    """
    EXACT SAME SPLIT STRATEGY AS RF.

    Scenes are separated completely so that
    spatially adjacent crops from the same scene
    cannot leak between train/validation/test.
    """

    scene_to_crops = defaultdict(list)

    for crop in crops:

        scene_to_crops[
            crop["scene"]
        ].append(crop)

    scenes = sorted(
        scene_to_crops.keys()
    )

    print()
    print("=" * 60)
    print("Creating scene-level stratified split...")
    print("=" * 60)

    stats = get_scene_statistics(
        crops
    )

    scenes_with_stats = sorted(
        scenes,
        key=lambda s:
            stats[s]["oil_ratio"],
    )

    rng = np.random.default_rng(
        RANDOM_STATE
    )

    # --------------------------------------------------------
    # Oil-ratio bins
    # --------------------------------------------------------

    bins = defaultdict(list)

    for scene in scenes_with_stats:

        ratio = stats[scene][
            "oil_ratio"
        ]

        if ratio == 0:

            bin_id = 0

        elif ratio < 0.01:

            bin_id = 1

        elif ratio < 0.05:

            bin_id = 2

        elif ratio < 0.15:

            bin_id = 3

        elif ratio < 0.30:

            bin_id = 4

        else:

            bin_id = 5

        bins[bin_id].append(
            scene
        )

    train_scenes = []
    val_scenes = []
    test_scenes = []

    # --------------------------------------------------------
    # Distribute each bin
    # --------------------------------------------------------

    for bin_id in sorted(
        bins.keys()
    ):

        bin_scenes = bins[
            bin_id
        ].copy()

        rng.shuffle(
            bin_scenes
        )

        n = len(
            bin_scenes
        )

        n_train = int(
            round(
                n * TRAIN_RATIO
            )
        )

        n_val = int(
            round(
                n * VAL_RATIO
            )
        )

        n_test = (
            n
            - n_train
            - n_val
        )

        train_scenes.extend(
            bin_scenes[
                :n_train
            ]
        )

        val_scenes.extend(
            bin_scenes[
                n_train:
                n_train + n_val
            ]
        )

        test_scenes.extend(
            bin_scenes[
                n_train + n_val:
            ]
        )

    # --------------------------------------------------------
    # Shuffle final lists
    # --------------------------------------------------------

    rng.shuffle(
        train_scenes
    )

    rng.shuffle(
        val_scenes
    )

    rng.shuffle(
        test_scenes
    )

    train_scene_set = set(
        train_scenes
    )

    val_scene_set = set(
        val_scenes
    )

    test_scene_set = set(
        test_scenes
    )

    train_crops = [
        crop
        for crop in crops
        if crop["scene"]
        in train_scene_set
    ]

    val_crops = [
        crop
        for crop in crops
        if crop["scene"]
        in val_scene_set
    ]

    test_crops = [
        crop
        for crop in crops
        if crop["scene"]
        in test_scene_set
    ]

    print()
    print("Scene split:")

    print(
        f"Train scenes:      "
        f"{len(train_scenes)}"
    )

    print(
        f"Validation scenes: "
        f"{len(val_scenes)}"
    )

    print(
        f"Test scenes:       "
        f"{len(test_scenes)}"
    )

    print()
    print("Crop split:")

    print(
        f"Train crops:      "
        f"{len(train_crops)}"
    )

    print(
        f"Validation crops: "
        f"{len(val_crops)}"
    )

    print(
        f"Test crops:       "
        f"{len(test_crops)}"
    )

    return (
        train_crops,
        val_crops,
        test_crops,
    )


# ============================================================
# CREATE TRAINING SAMPLES
# ============================================================

def extract_samples_from_crop(
    crop,
    samples_per_crop,
    normalization_mean=None,
    normalization_std=None,
    training=False,
):
    """
    Extract spatial-spectral windows.

    Each sample is:

        32 x 32 x 10

    The label is the CENTER pixel.

    Important:
        The center pixel must be a high-confidence
        labelled pixel.

        The entire window must contain finite
        spectral values.

    Neighbouring pixels do NOT have to be labelled.
    They are used as spatial context.
    """

    X, labels, confidence = (
        load_full_crop(crop)
    )

    if X is None:

        return [], []

    height, width, _ = X.shape

    radius = PATCH_SIZE // 2

    # --------------------------------------------------------
    # Valid center pixels
    # --------------------------------------------------------

    center_valid = (
        (labels > 0)
        &
        (confidence == 1)
        &
        np.all(
            np.isfinite(X),
            axis=-1,
        )
    )

    # Avoid border pixels because they cannot
    # produce a complete 32x32 window.
    center_valid[
        :radius,
        :
    ] = False

    center_valid[
        height - radius:,
        :
    ] = False

    center_valid[
        :,
        :radius
    ] = False

    center_valid[
        :,
        width - radius:
    ] = False

    rows, cols = np.where(
        center_valid
    )

    if len(rows) == 0:

        return [], []

    # --------------------------------------------------------
    # Deterministic sampling
    # --------------------------------------------------------

    if len(rows) > samples_per_crop:

        seed = (
            RANDOM_STATE
            + int(crop["patch"])
        )

        if training:

            # Add no random epoch variation.
            # Keeping this deterministic makes
            # the experiment reproducible.
            rng = np.random.default_rng(
                seed
            )

        else:

            rng = np.random.default_rng(
                seed
            )

        indices = rng.choice(
            len(rows),
            size=samples_per_crop,
            replace=False,
        )

        rows = rows[indices]
        cols = cols[indices]

    samples = []
    targets = []

    # --------------------------------------------------------
    # Extract windows
    # --------------------------------------------------------

    for row, col in zip(
        rows,
        cols,
    ):

        r0 = (
            row - radius
        )

        r1 = (
            r0 + PATCH_SIZE
        )

        c0 = (
            col - radius
        )

        c1 = (
            c0 + PATCH_SIZE
        )

        window = X[
            r0:r1,
            c0:c1,
            :
        ]

        # ----------------------------------------------------
        # Full-window finite check
        # ----------------------------------------------------

        if window.shape != (
            PATCH_SIZE,
            PATCH_SIZE,
            NUM_BANDS,
        ):

            continue

        if not np.all(
            np.isfinite(window)
        ):

            continue

        # ----------------------------------------------------
        # Normalize
        # ----------------------------------------------------

        if (
            normalization_mean
            is not None
            and normalization_std
            is not None
        ):

            window = (
                window
                - normalization_mean
            ) / normalization_std

        # ----------------------------------------------------
        # Convert HWC -> CHW
        # ----------------------------------------------------

        window = np.transpose(
            window,
            (2, 0, 1),
        )

        label = int(
            labels[row, col]
        )

        samples.append(
            window.astype(
                np.float32
            )
        )

        targets.append(
            label
        )

    return samples, targets


# ============================================================
# CALCULATE NORMALIZATION STATISTICS
# ============================================================

def calculate_normalization_stats(
    train_crops
):
    """
    Calculate mean/std using TRAIN ONLY.

    This prevents validation/test information
    from leaking into normalization.
    """

    print()
    print("=" * 60)
    print("CALCULATING TRAINING NORMALIZATION")
    print("=" * 60)

    band_sum = np.zeros(
        NUM_BANDS,
        dtype=np.float64,
    )

    band_sum_sq = np.zeros(
        NUM_BANDS,
        dtype=np.float64,
    )

    band_count = np.zeros(
        NUM_BANDS,
        dtype=np.int64,
    )

    for i, crop in enumerate(
        train_crops,
        start=1,
    ):

        X, labels, confidence = (
            load_full_crop(crop)
        )

        if X is None:

            continue

        valid = (
            (labels > 0)
            &
            (confidence == 1)
            &
            np.all(
                np.isfinite(X),
                axis=-1,
            )
        )

        pixels = X[valid]

        if len(pixels) == 0:

            continue

        band_sum += np.sum(
            pixels,
            axis=0,
            dtype=np.float64,
        )

        band_sum_sq += np.sum(
            pixels ** 2,
            axis=0,
            dtype=np.float64,
        )

        band_count += (
            len(pixels)
        )

        if i % 50 == 0:

            print(
                f"Processed "
                f"{i}/{len(train_crops)} "
                f"training crops"
            )

    if np.any(
        band_count == 0
    ):

        raise RuntimeError(
            "Could not calculate "
            "normalization statistics."
        )

    mean = (
        band_sum
        / band_count
    )

    variance = (
        band_sum_sq
        / band_count
        - mean ** 2
    )

    variance = np.maximum(
        variance,
        1e-12,
    )

    std = np.sqrt(
        variance
    )

    # Safety floor
    std = np.maximum(
        std,
        1e-6,
    )

    print()
    print("Training normalization:")

    for i, band in enumerate(
        FEATURE_NAMES
    ):

        print(
            f"{band:4s} "
            f"mean={mean[i]:.6f} "
            f"std={std[i]:.6f}"
        )

    return (
        mean.astype(np.float32),
        std.astype(np.float32),
    )


# ============================================================
# DATASET
# ============================================================

class MADOSCNNData(Dataset):

    def __init__(
        self,
        crops,
        samples_per_crop,
        mean,
        std,
        name,
    ):

        self.samples = []
        self.targets = []

        self.name = name

        print()
        print("=" * 60)
        print(
            f"BUILDING {name} DATASET"
        )
        print("=" * 60)

        for i, crop in enumerate(
            crops,
            start=1,
        ):

            X_samples, y_samples = (
                extract_samples_from_crop(
                    crop=crop,
                    samples_per_crop=samples_per_crop,
                    normalization_mean=mean,
                    normalization_std=std,
                    training=(
                        name == "TRAIN"
                    ),
                )
            )

            self.samples.extend(
                X_samples
            )

            self.targets.extend(
                y_samples
            )

            if i % 50 == 0:

                print(
                    f"Processed "
                    f"{i}/{len(crops)} crops | "
                    f"Samples: "
                    f"{len(self.samples):,}"
                )

        if len(self.samples) == 0:

            raise RuntimeError(
                f"No samples generated "
                f"for {name}."
            )

        self.samples = np.stack(
            self.samples
        ).astype(
            np.float32
        )

        # Convert MADOS 1..15
        # to PyTorch 0..14.
        self.targets = np.array(
            [
                CLASS_TO_INDEX[
                    int(label)
                ]
                for label in self.targets
            ],
            dtype=np.int64,
        )

        print()
        print(
            f"{name} dataset:"
        )

        print(
            f"Samples: "
            f"{len(self.targets):,}"
        )

        print(
            f"Shape: "
            f"{self.samples.shape}"
        )

    def __len__(self):

        return len(
            self.targets
        )

    def __getitem__(
        self,
        index,
    ):

        x = torch.from_numpy(
            self.samples[index]
        )

        y = torch.tensor(
            self.targets[index],
            dtype=torch.long,
        )

        return x, y


# ============================================================
# CLASS DISTRIBUTION
# ============================================================

def print_class_distribution(
    y,
    name,
):
    """
    y is PyTorch-style 0..14 here.
    """

    print()
    print(
        f"{name} CLASS DISTRIBUTION"
    )

    print("=" * 60)

    total = len(y)

    for class_id in CLASS_IDS:

        class_index = (
            class_id - 1
        )

        count = np.sum(
            y == class_index
        )

        percentage = (
            100.0 * count / total
            if total > 0
            else 0
        )

        print(
            f"{class_id:2d} "
            f"{CLASS_NAMES[class_id]:30s} "
            f"{count:8,} "
            f"({percentage:6.2f}%)"
        )


# ============================================================
# CLASS WEIGHTS
# ============================================================

def calculate_class_weights(
    y_train
):
    """
    Softer class weighting.

    sqrt(total / (classes * count))

    This avoids extreme weights for
    very rare classes.
    """

    counts = np.bincount(
        y_train,
        minlength=NUM_CLASSES,
    ).astype(
        np.float64
    )

    weights = np.zeros(
        NUM_CLASSES,
        dtype=np.float32,
    )

    total = len(
        y_train
    )

    for i in range(
        NUM_CLASSES
    ):

        if counts[i] > 0:

            weight = np.sqrt(
                total
                / (
                    NUM_CLASSES
                    * counts[i]
                )
            )

            weight = np.clip(
                weight,
                0.1,
                5.0,
            )

            weights[i] = weight

        else:

            weights[i] = 0.0

    return (
        counts,
        weights
    )


# ============================================================
# CNN MODEL
# ============================================================

class SpectralSpatialCNN(
    nn.Module
):

    def __init__(
        self,
        num_bands=NUM_BANDS,
        num_classes=NUM_CLASSES,
    ):

        super().__init__()

        self.features = nn.Sequential(

            nn.Conv2d(
                num_bands,
                32,
                kernel_size=3,
                padding=1,
            ),

            nn.BatchNorm2d(
                32
            ),

            nn.ReLU(),

            nn.Conv2d(
                32,
                64,
                kernel_size=3,
                padding=1,
            ),

            nn.BatchNorm2d(
                64
            ),

            nn.ReLU(),

            nn.MaxPool2d(
                2
            ),

            nn.Conv2d(
                64,
                128,
                kernel_size=3,
                padding=1,
            ),

            nn.BatchNorm2d(
                128
            ),

            nn.ReLU(),

            nn.MaxPool2d(
                2
            ),
        )

        self.classifier = nn.Sequential(

            nn.AdaptiveAvgPool2d(
                (1, 1)
            ),

            nn.Flatten(),

            nn.Dropout(
                0.3
            ),

            nn.Linear(
                128,
                num_classes,
            ),
        )

    def forward(self, x):

        x = self.features(
            x
        )

        x = self.classifier(
            x
        )

        return x


# ============================================================
# TRAIN ONE EPOCH
# ============================================================

def train_one_epoch(
    model,
    loader,
    criterion,
    optimizer,
):
    model.train()

    running_loss = 0.0

    correct = 0
    total = 0

    for X, y in loader:

        X = X.to(
            DEVICE,
            non_blocking=True,
        )

        y = y.to(
            DEVICE,
            non_blocking=True,
        )

        optimizer.zero_grad(
            set_to_none=True
        )

        logits = model(
            X
        )

        loss = criterion(
            logits,
            y,
        )

        loss.backward()

        optimizer.step()

        running_loss += (
            loss.item()
            * X.size(0)
        )

        predictions = (
            logits.argmax(
                dim=1
            )
        )

        correct += (
            predictions == y
        ).sum().item()

        total += (
            y.size(0)
        )

    epoch_loss = (
        running_loss
        / total
    )

    accuracy = (
        correct
        / total
    )

    return (
        epoch_loss,
        accuracy,
    )


# ============================================================
# EVALUATION
# ============================================================

@torch.no_grad()
def evaluate(
    model,
    loader,
):
    model.eval()

    all_predictions = []
    all_targets = []
    all_probabilities = []

    for X, y in loader:

        X = X.to(
            DEVICE,
            non_blocking=True,
        )

        logits = model(
            X
        )

        probabilities = torch.softmax(
            logits,
            dim=1,
        )

        predictions = (
            logits.argmax(
                dim=1
            )
        )

        all_predictions.append(
            predictions.cpu().numpy()
        )

        all_targets.append(
            y.numpy()
        )

        all_probabilities.append(
            probabilities.cpu().numpy()
        )

    y_pred = np.concatenate(
        all_predictions
    )

    y_true = np.concatenate(
        all_targets
    )

    probabilities = np.concatenate(
        all_probabilities
    )

    return (
        y_true,
        y_pred,
        probabilities,
    )


# ============================================================
# OIL METRICS
# ============================================================

def calculate_oil_metrics(
    y_true,
    y_pred,
):

    precision, recall, f1, _ = (
        precision_recall_fscore_support(
            y_true,
            y_pred,
            labels=[OIL_INDEX],
            average=None,
            zero_division=0,
        )
    )

    return (
        float(precision[0]),
        float(recall[0]),
        float(f1[0]),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # ========================================================
    # CHECK DATASET
    # ========================================================

    if not MADOS_ROOT.exists():

        raise FileNotFoundError(
            f"\nMADOS dataset not found:\n"
            f"{MADOS_ROOT}\n\n"
            f"Change MADOS_ROOT."
        )

    # ========================================================
    # HEADER
    # ========================================================

    print()
    print("=" * 60)
    print(
        "MADOS SENTINEL-2 CNN"
    )
    print(
        "FINAL 10-BAND "
        "SPATIAL-SPECTRAL VERSION"
    )
    print("=" * 60)

    print(
        f"\nModel will be saved to:"
    )

    print(
        MODEL_PATH
    )

    # ========================================================
    # FIND CROPS
    # ========================================================

    crops = get_crops(
        MADOS_ROOT
    )

    if len(crops) == 0:

        raise RuntimeError(
            "No complete MADOS patches found."
        )

    # ========================================================
    # SCENE SPLIT
    # ========================================================

    (
        train_crops,
        val_crops,
        test_crops,
    ) = split_by_scene(
        crops
    )

    # ========================================================
    # NORMALIZATION
    # ========================================================

    mean, std = (
        calculate_normalization_stats(
            train_crops
        )
    )

    # ========================================================
    # BUILD DATASETS
    # ========================================================

    train_dataset = (
        MADOSCNNData(
            train_crops,
            SAMPLES_PER_CROP,
            mean,
            std,
            "TRAIN",
        )
    )

    val_dataset = (
        MADOSCNNData(
            val_crops,
            SAMPLES_PER_CROP,
            mean,
            std,
            "VALIDATION",
        )
    )

    test_dataset = (
        MADOSCNNData(
            test_crops,
            SAMPLES_PER_CROP,
            mean,
            std,
            "TEST",
        )
    )

    # ========================================================
    # CLASS DISTRIBUTIONS
    # ========================================================

    print_class_distribution(
        train_dataset.targets,
        "TRAINING",
    )

    print_class_distribution(
        val_dataset.targets,
        "VALIDATION",
    )

    print_class_distribution(
        test_dataset.targets,
        "TEST",
    )

    # ========================================================
    # DATALOADERS
    # ========================================================

    train_loader = DataLoader(
        train_dataset,
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=NUM_WORKERS,
        pin_memory=torch.cuda.is_available(),
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=NUM_WORKERS,
        pin_memory=torch.cuda.is_available(),
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=NUM_WORKERS,
        pin_memory=torch.cuda.is_available(),
    )

    # ========================================================
    # CLASS WEIGHTS
    # ========================================================

    counts, class_weights = (
        calculate_class_weights(
            train_dataset.targets
        )
    )

    print()
    print("=" * 60)
    print("CLASS WEIGHTS")
    print("=" * 60)

    print(
        class_weights
    )

    class_weights_tensor = (
        torch.tensor(
            class_weights,
            dtype=torch.float32,
            device=DEVICE,
        )
    )

    # ========================================================
    # MODEL
    # ========================================================

    model = SpectralSpatialCNN(
        num_bands=NUM_BANDS,
        num_classes=NUM_CLASSES,
    ).to(
        DEVICE
    )

    print()
    print("=" * 60)
    print("MODEL")
    print("=" * 60)

    print(
        model
    )

    parameter_count = sum(
        p.numel()
        for p in model.parameters()
    )

    print()
    print(
        f"Parameters: "
        f"{parameter_count:,}"
    )

    # ========================================================
    # LOSS
    # ========================================================

    criterion = nn.CrossEntropyLoss(
        weight=class_weights_tensor
    )

    # ========================================================
    # OPTIMIZER
    # ========================================================

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
    )

    # ========================================================
    # LR SCHEDULER
    # ========================================================

    scheduler = (
        torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer,
            mode="max",
            factor=0.5,
            patience=3,
            min_lr=1e-6,
        )
    )

    # ========================================================
    # TRAINING HISTORY
    # ========================================================

    history = {

        "train_loss": [],

        "train_accuracy": [],

        "val_oil_f1": [],

        "learning_rate": [],
    }

    best_val_oil_f1 = -1.0

    best_epoch = -1

    # ========================================================
    # TRAIN
    # ========================================================

    print()
    print("=" * 60)
    print("TRAINING")
    print("=" * 60)

    if not EVAL_ONLY:
        for epoch in range(
            1,
            EPOCHS + 1,
        ):

            train_loss, train_accuracy = (
                train_one_epoch(
                    model,
                    train_loader,
                    criterion,
                    optimizer,
                )
            )

            # ----------------------------------------------------
            # Validation
            # ----------------------------------------------------

            (
                y_val,
                y_val_pred,
                val_probs,
            ) = evaluate(
                model,
                val_loader,
            )

            (
                val_precision,
                val_recall,
                val_oil_f1,
            ) = calculate_oil_metrics(
                y_val,
                y_val_pred,
            )

            # ----------------------------------------------------
            # Scheduler
            # ----------------------------------------------------

            scheduler.step(
                val_oil_f1
            )

            current_lr = (
                optimizer.param_groups[0][
                    "lr"
                ]
            )

            # ----------------------------------------------------
            # Save history
            # ----------------------------------------------------

            history[
                "train_loss"
            ].append(
                train_loss
            )

            history[
                "train_accuracy"
            ].append(
                train_accuracy
            )

            history[
                "val_oil_f1"
            ].append(
                val_oil_f1
            )

            history[
                "learning_rate"
            ].append(
                current_lr
            )

            # ----------------------------------------------------
            # Print
            # ----------------------------------------------------

            print()

            print(
                f"Epoch "
                f"{epoch:02d}/{EPOCHS}"
            )

            print(
                f"Train Loss: "
                f"{train_loss:.4f}"
            )

            print(
                f"Train Accuracy: "
                f"{train_accuracy:.4f}"
            )

            print(
                f"Validation Oil F1: "
                f"{val_oil_f1:.4f}"
            )

            print(
                f"Learning Rate: "
                f"{current_lr:.6f}"
            )

            # ----------------------------------------------------
            # Best model
            # ----------------------------------------------------

            if (
                val_oil_f1
                > best_val_oil_f1
            ):

                best_val_oil_f1 = (
                    val_oil_f1
                )

                best_epoch = epoch

                torch.save(
                    {
                        "model_state_dict":
                            model.state_dict(),

                        "feature_names":
                            FEATURE_NAMES,

                        "class_names":
                            CLASS_NAMES,

                        "num_bands":
                            NUM_BANDS,

                        "num_classes":
                            NUM_CLASSES,

                        "patch_size":
                            PATCH_SIZE,

                        "normalization_mean":
                            mean,

                        "normalization_std":
                            std,

                        "best_val_oil_f1":
                            best_val_oil_f1,

                        "best_epoch":
                            best_epoch,

                        "random_state":
                            RANDOM_STATE,

                        "samples_per_crop":
                            SAMPLES_PER_CROP,

                        "model_architecture":
                            "SpectralSpatialCNN",

                        "oil_class_id":
                            OIL_CLASS_ID,
                    },
                    MODEL_PATH,
                )

                print(
                    "  -> New best model"
                )

    # ========================================================
    # TRAINING COMPLETE
    # ========================================================

    print()
    print("=" * 60)
    print("TRAINING COMPLETE")
    print("=" * 60)

    print(
        f"Best epoch: "
        f"{best_epoch}"
    )

    print(
        f"Best validation Oil F1: "
        f"{best_val_oil_f1:.4f}"
    )

    print(
        f"Best model saved to:"
    )

    print(
        MODEL_PATH
    )

    # ========================================================
    # LOAD BEST MODEL
    # ========================================================

    checkpoint = torch.load(
        MODEL_PATH,
        map_location=DEVICE,
        weights_only=False
    )

    model.load_state_dict(
        checkpoint[
            "model_state_dict"
        ]
    )

    model.eval()

    print()
    print(
        "Loaded best checkpoint "
        f"(epoch {checkpoint['best_epoch']})"
    )

    # ========================================================
    # VALIDATION RESULTS
    # ========================================================

    (
        y_val,
        y_val_pred,
        val_probs,
    ) = evaluate(
        model,
        val_loader,
    )

    # Convert back to MADOS labels 1..15
    y_val_mados = (
        y_val + 1
    )

    y_val_pred_mados = (
        y_val_pred + 1
    )

    print()
    print("=" * 60)
    print("VALIDATION RESULTS")
    print("=" * 60)

    print(
        classification_report(
            y_val_mados,
            y_val_pred_mados,
            labels=CLASS_IDS,
            target_names=[
                CLASS_NAMES[i]
                for i in CLASS_IDS
            ],
            zero_division=0,
        )
    )

    (
        val_precision,
        val_recall,
        val_oil_f1,
    ) = calculate_oil_metrics(
        y_val,
        y_val_pred,
    )

    print()
    print("-" * 40)
    print(
        "OIL SPILL PERFORMANCE"
    )
    print("-" * 40)

    print(
        f"Precision: "
        f"{val_precision:.4f}"
    )

    print(
        f"Recall:    "
        f"{val_recall:.4f}"
    )

    print(
        f"F1:        "
        f"{val_oil_f1:.4f}"
    )

    # ========================================================
    # TEST RESULTS
    # ========================================================

    (
        y_test,
        y_test_pred,
        test_probs,
    ) = evaluate(
        model,
        test_loader,
    )

    y_test_mados = (
        y_test + 1
    )

    y_test_pred_mados = (
        y_test_pred + 1
    )

    print()
    print("=" * 60)
    print("TEST RESULTS")
    print("=" * 60)

    print(
        classification_report(
            y_test_mados,
            y_test_pred_mados,
            labels=CLASS_IDS,
            target_names=[
                CLASS_NAMES[i]
                for i in CLASS_IDS
            ],
            zero_division=0,
        )
    )

    (
        test_precision,
        test_recall,
        test_oil_f1,
    ) = calculate_oil_metrics(
        y_test,
        y_test_pred,
    )

    print()
    print("-" * 40)
    print(
        "OIL SPILL PERFORMANCE"
    )
    print("-" * 40)

    print(
        f"Precision: "
        f"{test_precision:.4f}"
    )

    print(
        f"Recall:    "
        f"{test_recall:.4f}"
    )

    print(
        f"F1:        "
        f"{test_oil_f1:.4f}"
    )

    # ========================================================
    # CONFUSION MATRIX
    # ========================================================

    cm = confusion_matrix(
        y_test_mados,
        y_test_pred_mados,
        labels=CLASS_IDS,
    )

    print()
    print(
        "Confusion matrix:"
    )

    print(
        cm
    )

    # ========================================================
    # OIL PROBABILITY ANALYSIS
    # ========================================================

    # Probability corresponding to MADOS class 6.
    oil_prob = (
        test_probs[:, OIL_INDEX]
    )

    actual_oil = (
        y_test == OIL_INDEX
    )

    actual_non_oil = (
        y_test != OIL_INDEX
    )

    oil_pixel_probs = (
        oil_prob[
            actual_oil
        ]
    )

    non_oil_pixel_probs = (
        oil_prob[
            actual_non_oil
        ]
    )

    print()
    print("=" * 60)
    print(
        "OIL PROBABILITY DISTRIBUTION"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # All pixels
    # --------------------------------------------------------

    print()
    print("ALL TEST PIXELS")

    print(
        f"Mean:    "
        f"{oil_prob.mean():.4f}"
    )

    print(
        f"Median:  "
        f"{np.median(oil_prob):.4f}"
    )

    print(
        f"Min:     "
        f"{oil_prob.min():.4f}"
    )

    print(
        f"Max:     "
        f"{oil_prob.max():.4f}"
    )

    # --------------------------------------------------------
    # Actual oil
    # --------------------------------------------------------

    print()
    print(
        "ACTUAL OIL PIXELS"
    )

    print(
        f"Count:   "
        f"{len(oil_pixel_probs):,}"
    )

    print(
        f"Mean:    "
        f"{oil_pixel_probs.mean():.4f}"
    )

    print(
        f"Median:  "
        f"{np.median(oil_pixel_probs):.4f}"
    )

    print(
        f"Min:     "
        f"{oil_pixel_probs.min():.4f}"
    )

    print(
        f"Max:     "
        f"{oil_pixel_probs.max():.4f}"
    )

    print(
        f"25th %:  "
        f"{np.percentile(oil_pixel_probs, 25):.4f}"
    )

    print(
        f"75th %:  "
        f"{np.percentile(oil_pixel_probs, 75):.4f}"
    )

    print(
        f"90th %:  "
        f"{np.percentile(oil_pixel_probs, 90):.4f}"
    )

    print(
        f"95th %:  "
        f"{np.percentile(oil_pixel_probs, 95):.4f}"
    )

    # --------------------------------------------------------
    # Actual non-oil
    # --------------------------------------------------------

    print()
    print(
        "ACTUAL NON-OIL PIXELS"
    )

    print(
        f"Count:   "
        f"{len(non_oil_pixel_probs):,}"
    )

    print(
        f"Mean:    "
        f"{non_oil_pixel_probs.mean():.4f}"
    )

    print(
        f"Median:  "
        f"{np.median(non_oil_pixel_probs):.4f}"
    )

    print(
        f"Min:     "
        f"{non_oil_pixel_probs.min():.4f}"
    )

    print(
        f"Max:     "
        f"{non_oil_pixel_probs.max():.4f}"
    )

    print(
        f"25th %:  "
        f"{np.percentile(non_oil_pixel_probs, 25):.4f}"
    )

    print(
        f"75th %:  "
        f"{np.percentile(non_oil_pixel_probs, 75):.4f}"
    )

    print(
        f"90th %:  "
        f"{np.percentile(non_oil_pixel_probs, 90):.4f}"
    )

    print(
        f"95th %:  "
        f"{np.percentile(non_oil_pixel_probs, 95):.4f}"
    )

    # ========================================================
    # PROBABILITY HISTOGRAM
    # ========================================================

    plt.figure(
        figsize=(9, 5)
    )

    plt.hist(
        non_oil_pixel_probs,
        bins=50,
        alpha=0.6,
        label="Actual Non-Oil",
        density=True,
    )

    plt.hist(
        oil_pixel_probs,
        bins=50,
        alpha=0.6,
        label="Actual Oil",
        density=True,
    )

    plt.xlabel(
        "Predicted P(Oil)"
    )

    plt.ylabel(
        "Density"
    )

    plt.title(
        "CNN Oil Probability Distribution"
    )

    plt.legend()

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.tight_layout()

    plt.show()

    # ========================================================
    # CALIBRATION CURVE
    # ========================================================

    actual_oil_binary = (
        actual_oil.astype(
            np.int32
        )
    )

    prob_true, prob_pred = (
        calibration_curve(
            actual_oil_binary,
            oil_prob,
            n_bins=10,
            strategy="quantile",
        )
    )

    plt.figure(
        figsize=(7, 7)
    )

    plt.plot(
        prob_pred,
        prob_true,
        marker="o",
        label="CNN",
    )

    plt.plot(
        [0, 1],
        [0, 1],
        linestyle="--",
        label="Perfect calibration",
    )

    plt.xlabel(
        "Mean Predicted Probability"
    )

    plt.ylabel(
        "Actual Oil Fraction"
    )

    plt.title(
        "CNN Oil Probability Calibration"
    )

    plt.legend()

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.tight_layout()

    plt.show()

    # ========================================================
    # TRAINING CURVES
    # ========================================================

    epochs = range(
        1,
        len(
            history[
                "train_loss"
            ]
        ) + 1,
    )

    # --------------------------------------------------------
    # Training loss
    # --------------------------------------------------------

    plt.figure(
        figsize=(8, 5)
    )

    plt.plot(
        epochs,
        history[
            "train_loss"
        ],
        marker="o",
    )

    plt.xlabel(
        "Epoch"
    )

    plt.ylabel(
        "Training Loss"
    )

    plt.title(
        "CNN Training Loss"
    )

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.tight_layout()

    plt.show()

    # --------------------------------------------------------
    # Training accuracy
    # --------------------------------------------------------

    plt.figure(
        figsize=(8, 5)
    )

    plt.plot(
        epochs,
        history[
            "train_accuracy"
        ],
        marker="o",
    )

    plt.xlabel(
        "Epoch"
    )

    plt.ylabel(
        "Training Accuracy"
    )

    plt.title(
        "CNN Training Accuracy"
    )

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.tight_layout()

    plt.show()

    # --------------------------------------------------------
    # Validation Oil F1
    # --------------------------------------------------------

    plt.figure(
        figsize=(8, 5)
    )

    plt.plot(
        epochs,
        history[
            "val_oil_f1"
        ],
        marker="o",
    )

    plt.axvline(
        best_epoch,
        linestyle="--",
        label=(
            f"Best epoch = "
            f"{best_epoch}"
        ),
    )

    plt.xlabel(
        "Epoch"
    )

    plt.ylabel(
        "Validation Oil F1"
    )

    plt.title(
        "CNN Validation Oil F1"
    )

    plt.legend()

    plt.grid(
        True,
        alpha=0.3,
    )

    plt.tight_layout()

    plt.show()

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    print()
    print("=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)

    print()
    print(
        f"Best epoch: "
        f"{best_epoch}"
    )

    print(
        f"Best validation Oil F1: "
        f"{best_val_oil_f1:.4f}"
    )

    print(
        f"Test Oil Precision: "
        f"{test_precision:.4f}"
    )

    print(
        f"Test Oil Recall: "
        f"{test_recall:.4f}"
    )

    print(
        f"Test Oil F1: "
        f"{test_oil_f1:.4f}"
    )

    print()
    print(
        "MODEL SAVED"
    )

    print(
        MODEL_PATH
    )

    print()
    print(
        "DONE"
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()