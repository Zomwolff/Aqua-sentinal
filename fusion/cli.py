import argparse
from pathlib import Path

from .pipeline import detect_oil_result


def main():
    parser = argparse.ArgumentParser(description="Detect oil-like pixels from SAR, EO, or both")
    parser.add_argument("--s1", type=Path, help="Sentinel-1 GeoTIFF")
    parser.add_argument("--s2", type=Path, help="Sentinel-2 GeoTIFF or separate-band directory")
    args = parser.parse_args()
    if args.s1 is None and args.s2 is None:
        parser.error("provide --s1, --s2, or both")
    result = detect_oil_result(args.s1, args.s2)
    print("OIL_SPILL_DETECTED" if result["oil_spill_detected"] else "NO_OIL_SPILL")
    print(f"mode={result['mode']} mask_shape={result['mask'].shape} oil_pixels={int(result['mask'].sum())} weights={result['weights']}")


if __name__ == "__main__":
    main()
