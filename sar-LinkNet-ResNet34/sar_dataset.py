"""
Sentinel-1-only Dataset/DataLoader for the Refined Deep-SAR SOS dataset.

Usage:
    from sar_dataset import get_dataloaders
    train_loader, val_loader = get_dataloaders(r"C:\\Users\\datha\\Desktop\\sar\\dataset\\SOS")

Run this file directly to sanity-check shapes, value ranges, and image/mask
alignment before you touch the model.
"""

from pathlib import Path
import numpy as np
from PIL import Image
import torch
from torch.utils.data import Dataset, DataLoader
import albumentations as A
from albumentations.pytorch import ToTensorV2


IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


class SARSentinelDataset(Dataset):
    """
    Loads Sentinel-1 image/mask pairs only (filenames starting with 'sentinel_').
    Matches by relative filename within the given split folder, not across
    the whole dataset, to avoid stem-collision bugs.
    """

    def __init__(self, root: str, split: str, transform: A.Compose = None):
        self.root = Path(root)
        self.split = split
        self.img_dir = self.root / "images" / split
        self.mask_dir = self.root / "masks" / split

        img_files = sorted(self.img_dir.glob("sentinel_*.png"))
        pairs = []
        missing = []
        for img_path in img_files:
            mask_path = self.mask_dir / img_path.name
            if mask_path.exists():
                pairs.append((img_path, mask_path))
            else:
                missing.append(img_path.name)

        if missing:
            print(f"[WARNING] {len(missing)} sentinel images in '{split}' have no matching mask. "
                  f"First few: {missing[:5]}")

        self.pairs = pairs
        self.transform = transform

        if len(self.pairs) == 0:
            raise RuntimeError(f"No sentinel_*.png image/mask pairs found in {self.img_dir}")

    def __len__(self):
        return len(self.pairs)

    def __getitem__(self, idx):
        img_path, mask_path = self.pairs[idx]

        image = np.array(Image.open(img_path).convert("RGB"))          # (H,W,3) uint8
        mask = np.array(Image.open(mask_path).convert("L"))            # (H,W)   uint8, {0,255}

        # Binarize mask -> {0,1}
        mask = (mask > 127).astype(np.float32)

        if self.transform:
            augmented = self.transform(image=image, mask=mask)
            image, mask = augmented["image"], augmented["mask"]
        else:
            image = torch.from_numpy(image.transpose(2, 0, 1)).float() / 255.0
            mask = torch.from_numpy(mask).float()

        mask = mask.unsqueeze(0) if mask.dim() == 2 else mask  # ensure [1,H,W]
        return image, mask


def get_transforms(train: bool):
    if train:
        return A.Compose([
            A.HorizontalFlip(p=0.5),
            A.VerticalFlip(p=0.5),
            A.RandomRotate90(p=0.5),
            A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
            ToTensorV2(),
        ])
    else:
        return A.Compose([
            A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
            ToTensorV2(),
        ])


def get_dataloaders(root: str, batch_size: int = 4, num_workers: int = 0):
    train_ds = SARSentinelDataset(root, "train", transform=get_transforms(train=True))
    val_ds = SARSentinelDataset(root, "val", transform=get_transforms(train=False))

    train_loader = DataLoader(
        train_ds, batch_size=batch_size, shuffle=True,
        num_workers=num_workers, pin_memory=True, drop_last=True,
    )
    val_loader = DataLoader(
        val_ds, batch_size=batch_size, shuffle=False,
        num_workers=num_workers, pin_memory=True,
    )
    return train_loader, val_loader


if __name__ == "__main__":
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\datha\Desktop\sar\dataset\SOS"

    train_loader, val_loader = get_dataloaders(root, batch_size=4, num_workers=0)

    print(f"Train batches: {len(train_loader)}  (dataset size: {len(train_loader.dataset)})")
    print(f"Val batches:   {len(val_loader)}  (dataset size: {len(val_loader.dataset)})")

    images, masks = next(iter(train_loader))
    print(f"Image batch shape: {images.shape}, dtype: {images.dtype}")
    print(f"Mask batch shape:  {masks.shape}, dtype: {masks.dtype}")
    print(f"Image min/max: {images.min().item():.3f}/{images.max().item():.3f}")
    print(f"Mask unique values: {torch.unique(masks).tolist()}")
    print(f"Oil-pixel fraction in this batch: {masks.mean().item():.4f}")