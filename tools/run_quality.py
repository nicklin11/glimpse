"""Run the rewritten stage 5 over a set of extracted frames and print the verdicts.

This is the check that matters: the previous version rejected two frames scoring 85 580 and
91 629 MEGE because their canvas is dark, and claimed a 12/16 pass rate on that basis. It
should now be 14/16, failing only the two measurably blank frames.

    python tools/run_quality.py /tmp/dyak-probe/b1/images
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dyak import quality as q  # noqa: E402


def main() -> None:
    frames = sorted(Path(sys.argv[1]).glob("*.png")) or sorted(Path(sys.argv[1]).glob("*.jpg"))
    if not frames:
        raise SystemExit(f"no frames in {sys.argv[1]}")
    settings = q.Settings()
    report = q.run(frames, Path("/tmp/qualityrun"), settings=settings)
    print()
    print(f"{'frame':22} {'gate':6}{'mege':>10}{'thr':>9} {'crop':22}{'content':16}{'edges':>8}")
    for f in report.frames:
        print(
            f"{f.name:22} {'PASS' if f.passed else 'FAIL':6}{f.mege:10.0f}{f.threshold:9.0f} "
            f"{f.crop_state:22}{f.content_type:16}{f.edges:8d}  {f.reason}"
        )
    fallbacks = [f.name for f in report.frames if f.crop_state == q.CROP_FALLBACK]
    print(f"\nthreshold {report.threshold:.0f}, full-frame fallback: {fallbacks}")


if __name__ == "__main__":
    main()
