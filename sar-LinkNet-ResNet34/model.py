"""LinkNet with a ResNet-34 encoder for binary oil-spill segmentation."""

import segmentation_models_pytorch as smp


def build_model():
    return smp.Linknet(
        encoder_name="resnet34",
        encoder_weights="imagenet",
        in_channels=3,
        classes=1,
    )
