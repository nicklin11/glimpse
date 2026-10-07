"""Measure three claims before accepting them. Not part of the package; run from the repo root.

A. Does stage 4's q=2 JPEG materially move the Laplacian variance the gate reads?
   PNG and JPEG are extracted from the same source raster at the same width, so the only
   difference is the encode. Both are measured through the real _raw_gray path.
B. What do the frames that estimate_box rejects actually score? The code already measures
   them (quality.py:378) and then throws the number away.
C. Is Laplacian variance confounded by content density the way Tenengrad claims to fix?
   Computed against Sobel edge count on the same rasters.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dyak import quality as q  # noqa: E402

VIDEO = Path("~/Videos/lectures/Оптимальные СУ/1_lecture_OCS.webm").expanduser()
IMAGES = Path("/tmp/dyak-probe/b1/images")
OUT = Path("/tmp/jpegprobe")
WIDTH = 1600


def extract(pts_ms: int, dest: Path, *, mjpeg_q: int | None) -> None:
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{pts_ms / 1000:.3f}",
        "-i",
        str(VIDEO),
        "-frames:v",
        "1",
        "-vf",
        f"scale={WIDTH}:-2:flags=lanczos",
    ]
    if mjpeg_q is not None:
        args += ["-q:v", str(mjpeg_q), "-pix_fmt", "yuvj420p"]
    args.append(str(dest))
    subprocess.run(args, check=True, capture_output=True)


def measure(path: Path, analyse_width: int = 640):
    w, h = q._dimensions(path)
    ah = max(3, round(analyse_width * h / w))
    raster = q._raw_gray(path, analyse_width, ah)
    sobel, edges = sobel_stats(raster, analyse_width, ah)
    return {
        "lap": q.laplacian_variance(raster, analyse_width, ah),
        "sobel": sobel,
        "edges": edges,
        "w": w,
        "h": h,
    }


def _g(data: bytes, i: int) -> int:
    return data[i]


def sobel_stats(gray: bytes, w: int, h: int, *, threshold: int = 60) -> tuple[float, int]:
    """Classic Tenengrad and the edge population behind it, in one pass.

    Returns (mean of Gx^2+Gy^2 over thresholded pixels, count of those pixels). The count is
    the whole point: if Laplacian variance tracks it, the measure is partly reading how much
    ink is on the slide, not how sharp that ink is.
    """
    total = 0.0
    count = 0
    thr_sq = threshold * threshold
    for y in range(1, h - 1):
        base = y * w
        for x in range(1, w - 1):
            i = base + x
            tl, t, tr = gray[i - w - 1], gray[i - w], gray[i - w + 1]
            lm, rm = gray[i - 1], gray[i + 1]
            bl, b, br = gray[i + w - 1], gray[i + w], gray[i + w + 1]
            gx = (tr + 2 * rm + br) - (tl + 2 * lm + bl)
            gy = (bl + 2 * b + br) - (tl + 2 * t + tr)
            mag = gx * gx + gy * gy
            if mag >= thr_sq:
                total += mag
                count += 1
    return (total / count if count else 0.0), count


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = q.Settings()
    rows = []
    for jpg in sorted(IMAGES.glob("*.jpg")):
        pts_ms = int(jpg.stem.split("_")[1])
        png = OUT / f"{jpg.stem}.png"
        j2 = OUT / f"{jpg.stem}.q2.jpg"
        if not png.is_file():
            extract(pts_ms, png, mjpeg_q=None)
        if not j2.is_file():
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(png),
                    "-q:v",
                    "2",
                    "-pix_fmt",
                    "yuvj420p",
                    str(j2),
                ],
                check=True,
                capture_output=True,
            )

        png_m = measure(png)
        jpg_m = measure(j2)
        stored = measure(jpg)

        box = q.estimate_box(jpg, settings)
        row = {
            "name": jpg.name,
            "pts": pts_ms,
            "png_bytes": png.stat().st_size,
            "q2_bytes": j2.stat().st_size,
            "lap_png": round(png_m["lap"], 1),
            "lap_q2": round(jpg_m["lap"], 1),
            "lap_stored": round(stored["lap"], 1),
            "jpeg_delta_pct": round(100 * (jpg_m["lap"] - png_m["lap"]) / png_m["lap"], 2),
            "sobel_png": round(png_m["sobel"], 1),
            "sobel_q2": round(jpg_m["sobel"], 1),
            "edges_png": png_m["edges"],
            "edges_q2": jpg_m["edges"],
            "edge_delta_pct": round(
                100 * (jpg_m["edges"] - png_m["edges"]) / max(1, png_m["edges"]), 2
            ),
            "has_box": box is not None,
            "box": box.as_dict() if box else None,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    (OUT / "results.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print("\n=== summary ===")
    print(f"frames: {len(rows)}")
    deltas = [r["jpeg_delta_pct"] for r in rows]
    print(f"lap delta png->q2: min {min(deltas):+.2f}%  max {max(deltas):+.2f}%")
    ed = [r["edge_delta_pct"] for r in rows]
    print(f"edge count delta : min {min(ed):+.2f}%  max {max(ed):+.2f}%")
    print("stored vs q2 discrepancy (extract-vs-rewrite):")
    for r in rows:
        if abs(r["lap_stored"] - r["lap_q2"]) > 1:
            print(f"  {r['name']}: stored {r['lap_stored']} q2 {r['lap_q2']}")
    print("\nno-box frames (currently rejected regardless of sharpness):")
    for r in rows:
        if not r["has_box"]:
            print(f"  {r['name']}  sharpness(png)={r['lap_png']}")
    print("\nwith-box frames: sharpness range")
    wb = [r["lap_png"] for r in rows if r["has_box"]]
    if wb:
        print(f"  min {min(wb):.1f}  max {max(wb):.1f}")


if __name__ == "__main__":
    main()
