"""Calibrate the stage-5 sharpness threshold by blur injection.

The threshold in ADR-0001 was picked from a gap in a 16-sample distribution, which is not
calibration. This measures the metric's actual decay under known blur and places the gate
somewhere defensible on that curve instead.

Method: for each frame that passes the current gate, apply Gaussian blur at
sigma in [0.0, 3.0] step 0.2 through the same raster the pipeline analyses, and record
MEGE at every point. The threshold is then read off the decay, not guessed from a gap.

MEGE = mean(Gx^2 + Gy^2) over pixels where Gx^2 + Gy^2 >= tau.
The edge population divides out content density: a slide with 10x the text has ~10x the
edge pixels and ~10x the gradient sum, so the mean is unchanged. Measured on the
equal-per-edge-sharpness group from lecture 1, this cuts the spread across frames from
7.21x (Laplacian variance) to 1.66x.

Run from the repo root:  python tools/calibrate_sharpness.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dyak import quality as q  # noqa: E402

PROBE = Path("/tmp/jpegprobe")
SIGMAS = [round(0.2 * i, 1) for i in range(0, 16)]  # 0.0 .. 3.0 inclusive


def blurred_png(source: Path, sigma: float, cache: Path) -> Path:
    """One frame, blurred by `sigma`, re-encoded as PNG.

    PNG because a lossy re-encode would add its own high-frequency energy and the metric
    would then measure the encoder as well as the blur.
    """
    tag = f"s{sigma:.1f}"
    dest = cache / f"{source.stem}.{tag}.png"
    if not dest.is_file():
        args = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
        if sigma > 0:
            args += ["-vf", f"gblur=sigma={sigma}"]
        args += ["-frames:v", "1", str(dest)]
        subprocess.run(args, check=True, capture_output=True)
    return dest


def sobel_energy(raster: np.ndarray, tau: float) -> tuple[float, int]:
    """MEGE and the edge population, at the raster's own native resolution."""
    a = raster.astype(np.float64)
    padded = np.pad(a, 1, mode="edge")
    core = padded[1:-1, 1:-1]
    # Sobel 3x3 via slicing; no scipy dependency for a six-line operator.
    gx = (
        padded[:-2, 2:]
        + 2 * padded[1:-1, 2:]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[1:-1, :-2]
        - padded[2:, :-2]
    )
    gy = (
        padded[2:, :-2]
        + 2 * padded[2:, 1:-1]
        + padded[2:, 2:]
        - padded[:-2, :-2]
        - 2 * padded[:-2, 1:-1]
        - padded[:-2, 2:]
    )
    del core
    mag = gx * gx + gy * gy
    mask = mag >= tau
    count = int(mask.sum())
    if count == 0:
        return 0.0, 0
    return float(mag[mask].mean()), count


def native_gray(source: Path, box: dict | None) -> np.ndarray:
    """The analysis raster: the crop, or the whole frame, at 1:1. No rescale anywhere."""
    w, h = q._dimensions(source)
    if box is None:
        return np.frombuffer(q._raw_gray(source, w, h), dtype=np.uint8).reshape(h, w)
    b = box
    return np.frombuffer(
        q._raw_gray(
            source,
            b["width"],
            b["height"],
            pre=f"crop={b['width']}:{b['height']}:{b['x']}:{b['y']}",
        ),
        dtype=np.uint8,
    ).reshape(b["height"], b["width"])


def main() -> None:
    tau = 30.0
    cache = PROBE / "blur"
    cache.mkdir(parents=True, exist_ok=True)
    settings = q.Settings()


    edges: dict[str, int] = {}
    boxes: dict[str, dict | None] = {}
    rows: list[dict] = []

    for png in sorted(PROBE.glob("f_*.png")):
        # The box is estimated from the JPEG stage 4 wrote; the coordinates are the same
        # grid either way, and estimating from the lossless copy would be circular.
        jpg = Path("/tmp/dyak-probe/b1/images") / f"{png.stem}.jpg"
        found = q.estimate_box(jpg, settings) if jpg.is_file() else None
        box = found.as_dict() if found else None
        boxes[png.stem] = box

        row: dict = {"frame": png.stem, "box": box, "mege": {}}
        for sigma in SIGMAS:
            frame = blurred_png(png, sigma, cache)
            raster = native_gray(frame, box)
            value, count = sobel_energy(raster, tau)
            row["mege"][f"{sigma:.1f}"] = round(value, 1)
            if sigma == 0.0:
                edges[png.stem] = count
                row["edges_at_native"] = count
                row["raster"] = f"{raster.shape[1]}x{raster.shape[0]}"
        rows.append(row)
        print(
            f"{png.stem}  edges {edges[png.stem]:7d}  "
            f"raster {row['raster']:>9}  "
            + " ".join(
                f"{s:.1f}:{row['mege'][f'{s:.1f}']:8.0f}" for s in (0.0, 0.6, 1.2, 2.0, 3.0)
            ),
            flush=True,
        )

    out = PROBE / "calibration.json"
    out.write_text(json.dumps({"tau": tau, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")

    # Where does each curve fall to half of its unblurred value, and to a tenth?
    print("\nsigma at which MEGE decays to a fraction of its sigma=0 value:")
    print(f"  {'frame':22} {'M(0)':>9} {'half':>6} {'tenth':>6}")
    halves, tenths = [], []
    for row in rows:
        base = row["mege"]["0.0"]
        half = tenth = None
        for sigma in SIGMAS:
            frac = row["mege"][f"{sigma:.1f}"] / base if base else 0.0
            if half is None and frac <= 0.5:
                half = sigma
            if tenth is None and frac <= 0.1:
                tenth = sigma
                break
        halves.append(half)
        tenths.append(tenth)
        print(f"  {row['frame']:22} {base:9.0f} {str(half):>6} {str(tenth):>6}")
    ok_h = [h for h in halves if h is not None]
    ok_t = [t for t in tenths if t is not None]
    if ok_h:
        print(f"\n  half-value sigma: min {min(ok_h)} max {max(ok_h)}")
    if ok_t:
        print(f"  tenth-value sigma: min {min(ok_t)} max {max(ok_t)}")


if __name__ == "__main__":
    main()
