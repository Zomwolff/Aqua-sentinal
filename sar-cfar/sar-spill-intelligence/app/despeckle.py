"""Vectorized Lee SAR despeckling (intensity/linear-power domain).

Speckle in SAR is a multiplicative noise process in the intensity (linear
power) domain, which is the domain the classic Lee (1980) minimum-mean-square
error filter is derived for. Sentinel-1 GRD products are delivered in dB
(10·log10(power)), so physically correct filtering is:

    dB -> linear power -> Lee -> dB

The conversion is explicit and lossless; ``domain="dB"`` keeps the legacy
in-dB approximation available (documented deviation), but ``"linear"`` is the
default because it is the research-correct convention.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter

_SUPPORTED_DOMAINS = ("linear", "dB")

# Floor used when converting linear power back to dB to avoid log10(0).
_POWER_FLOOR = 1e-12


def _validate_window_size(window_size: int) -> None:
    if not isinstance(window_size, int) or isinstance(window_size, bool):
        raise ValueError("window_size must be a positive odd integer.")
    if window_size <= 0 or window_size % 2 == 0:
        raise ValueError("window_size must be a positive odd integer.")


def _validate_domain(domain: str) -> None:
    if domain not in _SUPPORTED_DOMAINS:
        raise ValueError(
            f"domain must be one of {_SUPPORTED_DOMAINS} (got {domain!r})."
        )


def db_to_linear_power(image_db: np.ndarray) -> np.ndarray:
    """Convert decibel backscatter to linear power: P = 10^(dB / 10)."""
    return np.power(10.0, np.asarray(image_db, dtype=np.float64) / 10.0)


def linear_power_to_db(image_power: np.ndarray) -> np.ndarray:
    """Convert linear power back to decibel: dB = 10·log10(P)."""
    return 10.0 * np.log10(np.clip(np.asarray(image_power, dtype=np.float64), _POWER_FLOOR, None))


def _lee_intensity(values: np.ndarray, window_size: int) -> np.ndarray:
    """Classic Lee MMSE filter on intensity values.

    Weight per pixel: w = var_local / (var_local + var_noise), where the noise
    variance is estimated globally from the local-variance field. Fully
    vectorized via uniform filters.
    """
    local_mean = uniform_filter(values, size=window_size, mode="reflect")
    local_mean_sq = uniform_filter(
        values * values,
        size=window_size,
        mode="reflect",
    )
    local_variance = np.maximum(local_mean_sq - local_mean * local_mean, 0.0)
    noise_variance = float(np.mean(local_variance))
    epsilon = np.finfo(values.dtype).eps
    weights = local_variance / (local_variance + noise_variance + epsilon)

    return local_mean + weights * (values - local_mean)


def lee_filter(image: np.ndarray, window_size: int = 5, domain: str = "linear") -> np.ndarray:
    """Apply a vectorized Lee speckle filter.

    Parameters
    ----------
    image : np.ndarray
        2-D SAR backscatter. Interpreted according to ``domain``.
    window_size : int
        Side length of the square local-statistics window (odd). 5×5 retains
        more spill-boundary detail than 7×7, which risks eroding small slicks.
    domain : str
        ``"linear"`` (default): input is dB and is converted to linear power,
        filtered there (where speckle is multiplicative and the Lee model is
        valid), then converted back to dB — the physically correct pipeline.
        ``"dB"``: legacy in-dB local-statistics approximation, kept for
        reproducibility of earlier runs; it is NOT the research-standard
        formulation.

    Returns
    -------
    np.ndarray
        Despeckled image in the same unit as the input (dB in / dB out).
    """
    _validate_window_size(window_size)
    _validate_domain(domain)

    source = np.asarray(image)
    if source.size == 0:
        raise ValueError("image must not be empty.")
    if not np.issubdtype(source.dtype, np.number):
        raise ValueError("image must contain numeric values.")
    if not np.all(np.isfinite(source)):
        raise ValueError("image must contain only finite values.")

    values_db = source.astype(np.float64, copy=True)

    if domain == "dB":
        # Legacy behaviour: filter the dB values directly.
        return _lee_intensity(values_db, window_size)

    # Physically correct path: intensity-domain filtering with explicit
    # round-trip conversion.
    power = db_to_linear_power(values_db)
    filtered_power = _lee_intensity(power, window_size)
    return linear_power_to_db(filtered_power)
