"""Stage 2: extract a 16 kHz mono wav from the source recording.

16 kHz mono PCM is what whisper.cpp expects, so this is not a quality choice but
a contract with the consumer; the sample rate is asserted afterwards rather than
assumed from the ffmpeg arguments.

**The artefact is verified on disk, not inferred from the exit code.** ffmpeg
exiting 0 is not evidence that a wav exists: `-i` on a source whose audio stream
decodes to nothing produces a header-only file and a zero exit status, and the
pipeline that trusts the exit code will hand stage 3 a 44-byte file and call it a
transcript (the failure mode ADR-0001 D3 records, which shipped as a 27-minute
run that "succeeded" having produced nothing). So: existence, non-trivial size,
then a real ffprobe pass over the produced file.

`-map 0:a:0` is explicit on purpose. Without it ffmpeg picks the stream marked
default, which for a screen recording with an added microphone track is not
guaranteed to be the lecture audio; naming the index makes the choice visible and
reproducible instead of dependent on container metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import exitcodes as ec
from . import probe, runner

REMEDIATION = "install: pacman -S ffmpeg"

SAMPLE_RATE = 16000
CHANNELS = 1

# A valid RIFF/WAVE header with no data chunk payload. 44 bytes is the canonical
# minimum for PCM; anything at or below it carries no audio.
WAV_HEADER_BYTES = 44

# If the extracted wav is shorter than this fraction of the source, the audio
# stream we picked is probably not the lecture audio. Warned, not fatal: a
# recording that genuinely starts late is legitimate input.
DURATION_TOLERANCE = 0.95


@dataclass(frozen=True)
class AudioArtefact:
    path: Path
    duration: float
    sample_rate: int
    channels: int
    size_bytes: int

    def summary(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        return f"{self.path.name} {self.duration:.1f}s {self.sample_rate}Hz mono, {mb:.1f} MiB"


def extract(source: Path, dest: Path) -> AudioArtefact:
    """Extract 16 kHz mono PCM wav from `source` into `dest`. Verified on disk."""
    # Both paths resolved for the reason given in probe(): neither ffmpeg nor
    # ffprobe has a `--` end-of-options marker, so a path beginning with "-"
    # would be read as an option and this stage would fail on a file that
    # probed fine one stage earlier.
    argv = [
        "-nostdin",  # else ffmpeg reads the parent's stdin and can hang the run
        "-v",
        "error",  # stderr carries only errors, so "verbatim" stays short
        "-y",
        "-i",
        str(Path(source).resolve()),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(Path(dest).resolve()),
    ]
    runner.run("ffmpeg", argv, remediation=REMEDIATION, timeout=runner.DEFAULT_TIMEOUT * 30)
    return verify(dest)


def verify(dest: Path) -> AudioArtefact:
    """Assert the wav exists, carries audio, and is in the agreed format."""
    if not dest.exists():
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=f"ffmpeg exited 0 but wrote no file at {dest}",
            remediation="check ffmpeg's build supports pcm_s16le and the wav muxer",
        )

    size = dest.stat().st_size
    if size <= WAV_HEADER_BYTES:
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=(
                f"ffmpeg produced a {size}-byte wav at {dest}: a header with no audio. "
                "The exit code was 0, so this is why artefact counts are verified on disk"
            ),
            remediation="check the source has a decodable audio stream",
        )

    info = probe.probe(dest)
    if not info.has_audio:
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=f"{dest} has no audio stream after extraction",
            remediation="check the source has a decodable audio stream",
        )
    if info.duration <= 0:
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=f"{dest} reports duration 0 despite being {size} bytes",
            remediation="re-extract with -v debug to see what the demuxer produced",
        )
    if info.sample_rate != SAMPLE_RATE or info.channels != CHANNELS:
        raise runner.DependencyError(
            name="ffmpeg",
            code=ec.DEPENDENCY_FAILED,
            message=(
                f"{dest} is {info.sample_rate}Hz/{info.channels}ch, "
                f"expected {SAMPLE_RATE}Hz/{CHANNELS}ch"
            ),
            remediation="the ffmpeg resampler did not apply; check the build",
        )

    return AudioArtefact(
        path=dest,
        duration=info.duration,
        sample_rate=info.sample_rate or SAMPLE_RATE,
        channels=info.channels or CHANNELS,
        size_bytes=size,
    )


def check_coverage(info: probe.MediaInfo, audio: AudioArtefact) -> str | None:
    """Warn when the extracted audio is much shorter than the source.

    Returns the warning text, or None. Not fatal: `duration` for a screencast is
    sometimes the container's declared length rather than the last packet's.
    """
    if info.duration <= 0:
        return None
    ratio = audio.duration / info.duration
    if ratio < DURATION_TOLERANCE:
        return (
            f"extracted audio is {ratio:.0%} of the source duration "
            f"({audio.duration:.1f}s vs {info.duration:.1f}s); the selected audio "
            "stream may not be the lecture audio"
        )
    return None
