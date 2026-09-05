"""Epoch-level binary segmentation metrics computed from thresholded logits."""

import torch


class BinarySegmentationMetrics:
    def __init__(self, threshold: float = 0.5, eps: float = 1e-7):
        self.threshold = threshold
        self.eps = eps
        self.reset()

    def reset(self) -> None:
        self.true_positive = 0
        self.false_positive = 0
        self.false_negative = 0

    @torch.no_grad()
    def update(self, logits: torch.Tensor, targets: torch.Tensor) -> None:
        predictions = torch.sigmoid(logits) >= self.threshold
        targets = targets >= 0.5
        self.true_positive += torch.logical_and(predictions, targets).sum().item()
        self.false_positive += torch.logical_and(predictions, ~targets).sum().item()
        self.false_negative += torch.logical_and(~predictions, targets).sum().item()

    def compute(self) -> dict[str, float]:
        tp, fp, fn = self.true_positive, self.false_positive, self.false_negative
        precision = tp / (tp + fp + self.eps)
        recall = tp / (tp + fn + self.eps)
        iou = tp / (tp + fp + fn + self.eps)
        dice = 2 * tp / (2 * tp + fp + fn + self.eps)
        return {"iou": iou, "dice": dice, "precision": precision, "recall": recall, "f1": dice}
