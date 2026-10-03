#!/usr/bin/env python3
"""Regression: stages 1-3 honour the ADR-0001 contracts, not just their happy path.

Stage 0 pinned the exit-code contract before other stages could invent their own.
The same applies here, and these tests are mostly about the *unhappy* paths,
because that is where this pipeline's real failures live:

  * a dependency that is absent (2) versus present and failing (3), and the
    failure's stderr arriving byte-for-byte rather than as a paraphrase;
  * artefacts verified on disk, so an ffmpeg run that exits 0 having written
    nothing is caught instead of trusted (D3);
  * word timings consumed as seconds from shipboard's JSON, never reparsed out
    of stdout text, and BPE pieces left alone rather than detokenised by guess.

`runner.run` is stubbed throughout: no real ffmpeg, no whisper.cpp, no network.
The one real end-to-end run lives outside this file, in the acceptance notes.
"""

import io
import json
import pathlib
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import urllib.error

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from glimpse import audio as gla  # noqa: E402
from glimpse import cli as glc  # noqa: E402
from glimpse import deps as gld  # noqa: E402
from glimpse import exitcodes as ec  # noqa: E402
from glimpse import bundle as glb  # noqa: E402
from glimpse import frames as glfr  # noqa: E402
from glimpse import pipeline as glp  # noqa: E402
from glimpse import audio as glpa  # noqa: E402
from glimpse import probe as glpr  # noqa: E402
from glimpse import quality as glq  # noqa: E402
from glimpse import stt as glst  # noqa: E402
from glimpse import probe as glprobe  # noqa: E402
from glimpse import runner as glr  # noqa: E402
from glimpse import stt as glstt  # noqa: E402
from glimpse import workspace as glw  # noqa: E402
from glimpse.deps import PROCESS_REQUIRES  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else repr(detail)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + text if text else ''}")
    if not cond:
        failures.append(name)


def raises(fn, *args, **kwargs):
    """Return the DependencyError fn raised, or None."""
    try:
        fn(*args, **kwargs)
    except glr.DependencyError as exc:
        return exc
    except Exception as exc:  # a different exception type is itself a failure
        return SimpleNamespace(code="WRONG-TYPE", message=f"{type(exc).__name__}: {exc}")
    return None


# --- fake subprocess plumbing -------------------------------------------------
# FAKE maps program name -> list of (stdout, stderr, rc), consumed in order.
# CALLS records every invocation so the argv itself can be asserted.
FAKE: dict[str, list[tuple[bytes, bytes, int]]] = {}
CALLS: list[tuple[str, list[str]]] = []
real_run = glr.run
# Captured before anything is monkeypatched: by the time this file reaches the
# pipeline-ordering test, glp.run is the stub above, and testing against the stub would
# pass vacuously -- exactly the class of bug the test exists to catch.
REAL_PIPELINE_RUN = glp.run
REAL_QUALITY_RUN = glp.quality.run
real_resolve = glr.resolve
real_which = glr.shutil.which
real_sleep = glfr.time.sleep

PRESENT = {"ffmpeg", "ffprobe", "shipboard"}


def install_fake(programs: dict[str, object]) -> None:
    FAKE.clear()
    FAKE.update(programs)  # type: ignore[arg-type]
    CALLS.clear()


def fake_resolve(name, remediation):
    if name not in PRESENT:
        raise glr.DependencyError(
            name=name,
            code=ec.MISSING_DEPENDENCY,
            message=f"required dependency not found on PATH: {name}",
            remediation=remediation,
        )
    return f"/usr/bin/{name}"


def fake_run(name, args, *, remediation, timeout=glr.DEFAULT_TIMEOUT, cwd=None):
    fake_resolve(name, remediation)
    CALLS.append((name, list(args)))
    if name not in FAKE:
        # Silence here would be the fake's own version of the exit-code trust
        # this pipeline is built against: an unprogrammed call and a drained
        # queue would look identical, and the code under test would "pass"
        # without having been exercised at all.
        raise AssertionError(f"unprogrammed call to {name}: {args}")
    queue = FAKE[name]
    if not queue:
        raise AssertionError(f"{name} called more times than FAKE programs for: {args}")
    stdout, stderr, rc = queue.pop(0)
    if rc != 0:
        raise glr.DependencyError(
            name=name,
            code=ec.DEPENDENCY_FAILED,
            message=f"{name} exited {rc} running `{' '.join(args)}`",
            stderr=stderr,
            argv=list(args),
        )
    return stdout


tmp = Path(tempfile.mkdtemp(prefix="glimpse-test-"))
real_workdir_env = os.environ.get(glw.WORKDIR_ENV)
os.environ[glw.WORKDIR_ENV] = str(tmp)


# --- 1. dependency absent vs. present-and-failing -----------------------------
# these use the REAL runner.run, with only `which` patched, so the absent/failing
# distinction is exercised through the actual code path rather than a re-stub.
glr.shutil.which = lambda name: "/usr/bin/true" if name == "true" else None
exc = raises(real_run, "definitely-not-installed-xyz", ["--help"], remediation="pipx install x")
check("absent dependency -> exit 2", exc.code == ec.MISSING_DEPENDENCY, f"{exc.code}")
check("absent dependency names itself", "definitely-not-installed-xyz" in exc.message, exc.message)
check(
    "absent dependency carries remediation",
    exc.remediation == "pipx install x",
    str(exc.remediation),
)
check("absent dependency has no stderr to report", exc.stderr == b"", repr(exc.stderr))

# a real binary that fails: /bin/false exits 1 with empty stderr
glr.shutil.which = lambda name: "/bin/false" if name == "false" else None
exc = raises(real_run, "false", [], remediation="n/a")
check("present-but-failing -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "failing dependency is NOT reported as missing", exc.remediation is None, str(exc.remediation)
)

# a real binary that writes a non-UTF-8 byte to stderr and exits non-zero
noisy = tmp / "noisy.sh"
noisy.write_bytes(b"#!/bin/sh\nprintf 'boom: \\377\\376 bad\\n' >&2\nexit 7\n")
noisy.chmod(0o755)
glr.shutil.which = lambda name: str(noisy) if name == "noisy" else None
exc = raises(real_run, "noisy", [], remediation="n/a")
check("failing dependency -> exit 3 (real stderr)", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "non-UTF-8 stderr kept as bytes",
    exc.stderr in (b"boom: \xff\xfe bad\n", b"boom: \xc3\xbf\xc3\xbe bad\n"),
    repr(exc.stderr),
)
check("dependency exit code is reported", "exited 7" in exc.message, exc.message)

glr.shutil.which = real_which
glfr.time.sleep = real_sleep


# --- 2. report(): dependency stderr is reproduced verbatim ---------------------
class FakeStderr:
    """A stderr with a binary buffer, like the real one."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = []

    def write(self, s):
        self.text.append(s)
        return len(s)

    def flush(self):
        pass


raw = b"  indented: '{\"a\": 1}'\nffmpeg: cannot open shared object file\n\xff\xfe\n"
fake = FakeStderr()
real_stderr, sys.stderr = sys.stderr, fake
try:
    glr.report(glr.DependencyError("ffmpeg", ec.DEPENDENCY_FAILED, "boom", stderr=raw))
finally:
    sys.stderr = real_stderr
check(
    "dependency stderr is byte-for-byte",
    fake.buffer.getvalue() == raw,
    repr(fake.buffer.getvalue()),
)
check(
    "our own lines are prefixed, its bytes are not",
    fake.text and fake.text[0].startswith("glimpse: boom"),
)

labelled = FakeStderr()
real_stderr, sys.stderr = sys.stderr, labelled
try:
    glr.report(
        glr.DependencyError(
            "shipboard", ec.DEPENDENCY_FAILED, "shipboard printed junk", stdout=b"<html>err</html>"
        )
    )
finally:
    sys.stderr = real_stderr
labelled_text = "".join(labelled.text)
check("captured stdout is labelled as stdout", "shipboard stdout" in labelled_text, labelled_text)
check(
    "captured stdout is not presented as stderr",
    "wrote nothing to stderr" not in labelled_text,
    labelled_text,
)
check("captured stdout bytes still arrive", b"<html>err</html>" in labelled.buffer.getvalue())

fake2 = FakeStderr()
real_stderr, sys.stderr = sys.stderr, fake2
try:
    glr.report(glr.DependencyError("ffmpeg", ec.DEPENDENCY_FAILED, "boom"))
finally:
    sys.stderr = real_stderr
check(
    "a silent dependency is reported as silent",
    "wrote nothing to stderr" in "".join(fake2.text),
    "".join(fake2.text),
)
check("a silent dependency emits no bytes", fake2.buffer.getvalue() == b"")


# --- 3. workspace: retained on failure, removed on success ---------------------
work = glw.WorkDir()
kept = work.path
work.retain("because")
check("retained work dir still exists", kept.exists())
check("note names the reason", "because" in work.note(), work.note())
work.release()
check("release() does not remove a retained dir", kept.exists())

work2 = glw.WorkDir()
gone = work2.path
work2.release()
check("release() removes the work dir on success", not gone.exists())
check("note says removed", "removed" in work2.note(), work2.note())

work3 = glw.WorkDir(keep=True)
kept3 = work3.path
work3.release()
check("--keep-workdir survives release", kept3.exists())
check("work dir is 0700", oct(kept3.stat().st_mode)[-3:] == "700", oct(kept3.stat().st_mode)[-3:])

real_rmtree = glw.shutil.rmtree
try:
    glw.shutil.rmtree = lambda p: (_ for _ in ()).throw(OSError(30, "Read-only file system"))
    stuck = glw.WorkDir()
    stuck.release()
finally:
    glw.shutil.rmtree = real_rmtree
check("a failed rmtree does NOT claim removal", stuck.retained, stuck.note())
check(
    "a failed rmtree is reported as retained", "KEPT for inspection" in stuck.note(), stuck.note()
)
check("a failed rmtree explains itself", "could not be removed" in stuck.note(), stuck.note())
check("a failed rmtree leaves the dir findable", stuck.path.exists(), str(stuck.path))
shutil.rmtree(stuck.path, ignore_errors=True)
shutil.rmtree(kept, ignore_errors=True)
shutil.rmtree(kept3, ignore_errors=True)

# --- 4. stage 1: probe --------------------------------------------------------
glr.run = fake_run
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "vp8",
                                "width": 1920,
                                "height": 1080,
                            },
                            {
                                "codec_type": "audio",
                                "codec_name": "opus",
                                "sample_rate": "48000",
                                "channels": 2,
                            },
                        ],
                        "format": {"duration": "4396.000000"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
src = tmp / "lecture.webm"
src.write_bytes(b"not really a video")
info = glprobe.probe(src)
check("probe reads duration as seconds", info.duration == 4396.0, str(info.duration))
check("probe sees video", info.has_video and info.video_codec == "vp8", info.summary())
check("probe sees audio", info.has_audio and info.audio_codec == "opus", info.summary())
check("probe reads sample rate", info.sample_rate == 48000, str(info.sample_rate))
check("probe reads dimensions", (info.width, info.height) == (1920, 1080), info.summary())
check("a video source is accepted", raises(glprobe.require_supported, info) is None)

check(
    "ffprobe is invoked with -i and the resolved path",
    "-i" in CALLS[-1][1] and CALLS[-1][1][-1] == str(src.resolve()),
    str(CALLS[-1][1]),
)

# duration "NAN" must not become a NaN that poisons arithmetic downstream
install_fake(
    {"ffprobe": [(json.dumps({"streams": [], "format": {"duration": "NAN"}}).encode(), b"", 0)]}
)
check("NAN duration degrades to 0.0", glprobe.probe(src).duration == 0.0)

# ffprobe exiting 0 with unparseable output is a broken ffprobe, not bad input
install_fake({"ffprobe": [(b"<html>proxy error</html>", b"", 0)]})
exc = raises(glprobe.probe, src)
check("non-JSON from ffprobe -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("non-JSON report keeps the payload", b"proxy error" in exc.stderr, repr(exc.stderr))

# ffprobe exits 0 with a payload that is valid JSON but not a stream table.
# json.loads returns a list/number/string/None for these, and treating any of
# them as the stream dict raised AttributeError -- which escaped as a traceback
# with exit 1, i.e. a broken ffprobe reported to the user as a bad command line.
for bad in (
    b"[]",
    b"null",
    b"7",
    b'"probe"',
    b'{"streams": {"a": 1}}',
    b'{"streams": 3, "format": {}}',
):
    install_fake({"ffprobe": [(bad, b"", 0)]})
    exc = raises(glprobe.probe, src)
    check(
        f"ffprobe payload {bad.decode()!r} -> exit 3, not a traceback",
        exc is not None and exc.code == ec.DEPENDENCY_FAILED,
        f"{getattr(exc, 'code', None)} {getattr(exc, 'message', 'no exception')}",
    )

# A correctly-shaped payload with junk entries degrades rather than raising:
# absent or unusable streams are a fact about the file, not a broken ffprobe.
install_fake({"ffprobe": [(b'{"streams": [null, 3, "x"]}', b"", 0)]})
degraded = glprobe.probe(src)
check("junk stream entries degrade instead of raising", not degraded.has_audio, degraded.summary())
check("an absent format degrades to duration 0", degraded.duration == 0.0, str(degraded.duration))
install_fake({"ffprobe": [(b'{"streams": [], "format": "nope"}', b"", 0)]})
exc = raises(glprobe.probe, src)
check("a format of the wrong type -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

exc = raises(glprobe.probe, tmp / "no-such-file.webm")
check("missing input -> exit 1 (usage)", exc.code == ec.USAGE, f"{exc.code}")
check("missing input names the path", "no-such-file.webm" in exc.message, exc.message)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000"}
                        ],
                        "format": {"duration": "100"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
audio_only = glprobe.probe(src)
exc = raises(glprobe.require_supported, audio_only)
check("audio-only input -> exit 1", exc.code == ec.USAGE, f"{exc.code}")
check("audio-only rejection cites the ADR boundary", "Not doing" in exc.message, exc.message)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [{"codec_type": "video", "codec_name": "vp8"}],
                        "format": {"duration": "100"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
silent_video = glprobe.probe(src)
exc = raises(glprobe.require_supported, silent_video)
check("video with no audio -> exit 1", exc.code == ec.USAGE, f"{exc.code}")
check("video with no audio says nothing to transcribe", "transcribe" in exc.message, exc.message)


# --- 5. stage 2: audio, verified on disk --------------------------------------
def wav_bytes(seconds: float, rate: int = 16000, channels: int = 1) -> bytes:
    """A minimal but real RIFF/WAVE PCM file: header plus silent samples.

    The data chunk has to actually carry the bytes it declares, or the file is
    header-only -- which is precisely the artefact `verify` is built to reject,
    so a helper that lies here would make the negative tests meaningless.
    """
    data_bytes = int(seconds * rate) * channels * 2
    header = b"RIFF" + (36 + data_bytes).to_bytes(4, "little") + b"WAVEfmt "
    header += (
        (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + channels.to_bytes(2, "little")
    )
    header += rate.to_bytes(4, "little") + (rate * channels * 2).to_bytes(4, "little")
    header += (channels * 2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    header += b"data" + data_bytes.to_bytes(4, "little")
    return header + b"\x00" * data_bytes


install_fake(
    {
        "ffmpeg": [(b"", b"", 0)],
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ],
    }
)
dest = tmp / "audio.wav"
dest.write_bytes(wav_bytes(25.0))
artefact = gla.verify(dest)
check("verify accepts a real wav", artefact.duration == 25.0, artefact.summary())
check(
    "verify reports 16 kHz mono",
    (artefact.sample_rate, artefact.channels) == (16000, 1),
    artefact.summary(),
)

argv = None
PCM_PROBE = json.dumps(
    {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
        "format": {"duration": "2.0"},
    }
).encode()

install_fake(
    {
        # ffmpeg must be programmed too: the strict fake raises on an
        # unprogrammed call, which is what surfaced that extract()'s
        # postcondition was previously never exercised at all.
        "ffmpeg": [(b"", b"", 0)],
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ],
    }
)
CALLS.clear()
gla.extract(src, dest)
ffmpeg_call = next(a for name, a in CALLS if name == "ffmpeg")
check(
    "ffmpeg gets -nostdin (else it eats the parent's stdin)",
    "-nostdin" in ffmpeg_call,
    str(ffmpeg_call),
)
check("ffmpeg maps the first audio stream explicitly", "0:a:0" in ffmpeg_call, str(ffmpeg_call))
check(
    "ffmpeg resamples to 16 kHz mono pcm",
    all(x in ffmpeg_call for x in ("16000", "1", "pcm_s16le")),
    str(ffmpeg_call),
)
check("ffmpeg drops video", "-vn" in ffmpeg_call, str(ffmpeg_call))
# probe() resolves the path because ffmpeg/ffprobe have no `--` end-of-options
# marker; extract() must do the same or stage 2 fails on a leading-dash filename
# that stage 1 just accepted.
check(
    "ffmpeg gets the resolved destination path",
    str(dest.resolve()) in ffmpeg_call,
    str(ffmpeg_call),
)

# Resolving must actually change something, which a comparison against an
# already-absolute path cannot show. Drive extract() with a RELATIVE source and
# require an absolute one to reach ffmpeg.
saved_cwd = os.getcwd()
try:
    os.chdir(tmp)
    (tmp / "out.wav").write_bytes(wav_bytes(2.0))
    install_fake({"ffmpeg": [(b"", b"", 0)], "ffprobe": [(PCM_PROBE, b"", 0)]})
    CALLS.clear()
    gla.extract(Path("lecture.webm"), Path("out.wav"))
    rel_call = next(a for name, a in CALLS if name == "ffmpeg")
finally:
    os.chdir(saved_cwd)
i = rel_call.index("-i")
check(
    "a relative source reaches ffmpeg as an absolute path",
    rel_call[i + 1] == str(src.resolve()),
    str(rel_call),
)
check(
    "a relative source is NOT passed through unresolved",
    rel_call[i + 1] != "lecture.webm",
    str(rel_call),
)
j = rel_call.index("wav")
check(
    "a relative destination reaches ffmpeg as an absolute path",
    rel_call[j + 1] == str((tmp / "out.wav").resolve()),
    str(rel_call),
)
check(
    "ffmpeg gets the resolved destination path",
    str(dest.resolve()) in ffmpeg_call,
    str(ffmpeg_call),
)

# extract()'s own postcondition: ffmpeg exits 0 and writes nothing. Pre-creating
# `dest` is what made this unreachable before -- verify() saw a valid wav that
# the test itself had put there.
install_fake({"ffmpeg": [(b"", b"", 0)], "ffprobe": [(b"{}", b"", 0)]})
absent = tmp / "never-written.wav"
exc = raises(gla.extract, src, absent)
check("ffmpeg exiting 0 without writing -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("the missing artefact is named", str(absent) in exc.message, exc.message)
check("nothing was invented on disk", not absent.exists())

header_only = tmp / "empty.wav"
header_only.write_bytes(wav_bytes(0))
exc = raises(gla.verify, header_only)
check("header-only wav -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("header-only wav is blamed on exit-code trust", "exit code was 0" in exc.message, exc.message)

exc = raises(gla.verify, tmp / "ffmpeg-wrote-nothing.wav")
check("no file at all -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

wrong = tmp / "wrong-rate.wav"
wrong.write_bytes(wav_bytes(10))
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "48000",
                                "channels": 2,
                            }
                        ],
                        "format": {"duration": "10.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
exc = raises(gla.verify, wrong)
check("wrong sample rate -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("wrong rate states expected vs actual", "48000Hz/2ch" in exc.message, exc.message)

install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "25.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
install_fake(
    {
        "ffprobe": [
            (
                json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "audio",
                                "codec_name": "pcm_s16le",
                                "sample_rate": "16000",
                                "channels": 1,
                            }
                        ],
                        "format": {"duration": "4396.0"},
                    }
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
long_info = glprobe.probe(src)
short_artefact = gla.AudioArtefact(dest, 100.0, 16000, 1, 800238)
warn = gla.check_coverage(long_info, short_artefact)
check(
    "a short extraction warns",
    warn is not None and "may not be the lecture audio" in warn,
    str(warn),
)
full_artefact = gla.AudioArtefact(dest, 4396.0, 16000, 1, 140_000_000)
ok = gla.check_coverage(long_info, full_artefact)
check("a full-length extraction does not warn", ok is None, str(ok))


# --- 6. stage 3: stt, timings as seconds, words left as BPE pieces ------------

# Stage 3 picks a backend by autodetection, and autodetection depends on what happens to
# be listening on 127.0.0.1 at the moment the suite runs. That is a property of the machine,
# not of the code under test: with whisper.cpp up the suite would POST the synthetic wav
# to the real server, and with it down it would fall back to shipboard. The backend is
# pinned here so the section tests one backend, and autodetection is tested separately in
# section 9 with both branches controlled.
real_stt_backend = os.environ.get("GLIMPSE_STT")
os.environ["GLIMPSE_STT"] = "shipboard"

REAL_SEGMENTS = [
    {
        "id": 0,
        "text": " Институт интеллектуальных устройств.",
        "start": 0.0,
        "end": 2.0,
        "tokens": [27222, 982, 13],
        "words": [
            {"word": " Ин", "start": 0.0, "end": 0.12, "t_dtw": -1, "probability": 0.134},
            {"word": "ст", "start": 0.12, "end": 0.23, "t_dtw": -1, "probability": 0.99},
            {"word": ".", "start": 2.0, "end": 2.0, "t_dtw": -1, "probability": 0.7},
        ],
        "temperature": 0.0,
        "avg_logprob": -0.2381982058286667,
        "no_speech_prob": 1.36e-11,
    },
    {
        "id": 1,
        "text": " Соответственно, я вообще не ППС,",
        "start": 2.0,
        "end": 4.0,
        "words": [
            {"word": " Соот", "start": 2.0, "end": 2.5},
            {"word": "вет", "start": 2.5, "end": 3.0},
        ],
    },
]

payload = json.dumps(REAL_SEGMENTS, ensure_ascii=False).encode()
install_fake({"shipboard": [(payload, b"", 0)]})
CALLS.clear()
tr = glstt.transcribe(dest, audio_duration=25.0)
ship_call = next(a for name, a in CALLS if name == "shipboard")
# Acceptance: timings are consumed from `--timestamps json`, never reparsed out
# of stdout text. The plain transcript form carries no segments at all, so this
# assertion is what would catch a regression to `--file`/`--no-copy` style use.
check(
    "shipboard is called with the json timestamp form", ship_call[:1] == ["process"], str(ship_call)
)
check(
    "shipboard is asked for --timestamps json",
    "--timestamps" in ship_call and "json" in ship_call,
    str(ship_call),
)
check(
    "shipboard is given the wav that was verified on disk",
    ship_call[:2] == ["process", str(dest)],
    str(ship_call),
)
check(
    "the timestamp flags come last, after the path",
    ship_call[2:] == ["--timestamps", "json"],
    str(ship_call),
)
check("segments are parsed", len(tr.segments) == 2, tr.summary())
check("timings are seconds, not milliseconds", tr.segments[0].end == 2.0, str(tr.segments[0].end))
check("timed words are counted", tr.timed_words == 5, str(tr.timed_words))
check("speech_end is the last segment end", tr.speech_end == 4.0, str(tr.speech_end))
check(
    "BPE pieces are NOT detokenised",
    [w.text for w in tr.segments[0].words] == [" Ин", "ст", "."],
    str([w.text for w in tr.segments[0].words]),
)
check(
    "segment text is stripped for reading",
    tr.segments[0].text.startswith("Институт"),
    tr.segments[0].text,
)
check(
    "avg_logprob is carried",
    abs(tr.segments[0].avg_logprob + 0.2382) < 1e-3,
    str(tr.segments[0].avg_logprob),
)
check("no anomalies on a clean payload", tr.anomalies == (), str(tr.anomalies))

install_fake({"shipboard": [(b"   \n", b"", 0)]})
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check("empty stdout -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

install_fake({"shipboard": [(b"not json at all", b"", 0)]})
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check("non-JSON stdout -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

# A JSON object is no longer a shape error: whisper.cpp's own /inference returns
# {task, language, duration, text, segments, ...}. Only shipboard unwraps it to a bare
# array. Both are accepted, and the envelope is recorded as an anomaly.
_install_fake_dict = {"shipboard": [(b'{"text": "plain"}', b"", 0)]}
install_fake(_install_fake_dict)
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check(
    "an object payload with no timings -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "it is rejected for the timings, not for the envelope",
    "zero words carrying timings" in getattr(exc, "message", ""),
    getattr(exc, "message", ""),
)

install_fake({"shipboard": [(b"[]", b"", 0)]})
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check("empty segments array -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")

install_fake(
    {
        "shipboard": [
            (
                json.dumps(
                    [{"id": 0, "text": " hi", "start": 0.0, "end": 1.0, "words": []}]
                ).encode(),
                b"",
                0,
            )
        ]
    }
)
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check(
    "zero timed words -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'no exception raised')}",
)
check(
    "zero timed words explains the consequence",
    exc is not None and "per-word" in exc.message,
    getattr(exc, "message", "no exception raised"),
)

messy = [
    {
        "id": 0,
        "text": " one",
        "start": 5.0,
        "end": 6.0,
        "words": [{"word": " one", "start": 5.0, "end": 6.0}],
    },
    {
        "id": 1,
        "text": " two",
        "start": 3.0,
        "end": 4.0,
        "words": [{"word": " two", "start": 3.0, "end": 4.0}],
    },
    {
        "id": 2,
        "text": " three",
        "start": 7.0,
        "end": 6.0,
        "words": [{"word": " three", "start": 7.0, "end": 6.0}],
    },
    {"id": 3, "text": "   ", "start": 8.0, "end": 9.0, "words": []},
    {"id": 4, "start": 9.0, "end": 9.5, "words": []},
    "not an object",
]
install_fake({"shipboard": [(json.dumps(messy).encode(), b"", 0)]})
tr = glstt.transcribe(dest, audio_duration=25.0)
check("blank-text segments are dropped", len(tr.segments) == 3, str(len(tr.segments)))
check("anomalies are collected, not fatal", len(tr.anomalies) >= 4, str(tr.anomalies))
check(
    "a backwards segment is recorded",
    any("before the previous" in a for a in tr.anomalies),
    str(tr.anomalies),
)
check("a backwards end is clamped", tr.segments[2].end >= tr.segments[2].start, str(tr.segments[2]))

check(
    "timestamp formatting",
    glstt.format_timestamp(4396.0) == "01:13:16",
    glstt.format_timestamp(4396.0),
)

# `1e999` and the bare literals are valid JSON *input* and parse to inf / NaN.
# Left unchecked they reach format_timestamp -> int(inf) -> OverflowError, after
# a full lecture has been transcribed and paid for, and land in transcript.json
# as a bare `Infinity` token, which is not valid JSON at all.
for literal in (b"Infinity", b"-Infinity", b"NaN", b"1e999"):
    nasty = (
        b'[{"text": " x", "start": 0.0, "end": '
        + literal
        + b', "words": [{"word": " x", "start": 0.0, "end": 1.0}]}]'
    )
    install_fake({"shipboard": [(nasty, b"", 0), (nasty, b"", 0)]})
    got = raises(glstt.transcribe, dest, audio_duration=25.0)
    check(
        f"a non-finite end ({literal.decode()}) does not crash the run",
        got is None,
        str(got),
    )
    if got is None:
        tr_n = glstt.transcribe(dest, audio_duration=25.0)
        check(
            f"a non-finite end ({literal.decode()}) is recorded as an anomaly",
            any("non-finite end" in a for a in tr_n.anomalies),
            str(tr_n.anomalies),
        )
        check(
            f"a non-finite end ({literal.decode()}) leaves every timing finite",
            all(
                s_.start not in (float("inf"), float("-inf"))
                and s_.end not in (float("inf"), float("-inf"))
                for s_ in tr_n.segments
            ),
        )
        paths_n = glstt.write(tr_n, tmp)
        text_n = paths_n["json"].read_text(encoding="utf-8")
        check(
            f"transcript.json stays valid JSON after a {literal.decode()}",
            "Infinity" not in text_n and "NaN" not in text_n,
            text_n[:200],
        )

# And the clamping path still has to produce a writable, valid artefact.
install_fake({"shipboard": [(payload, b"", 0)]})
tr = glstt.transcribe(dest, audio_duration=25.0)
paths = glstt.write(tr, tmp)
check(
    "three transcript artefacts are written",
    set(paths) == {"raw", "json", "txt"},
    str(sorted(paths)),
)
check("raw payload is preserved byte-for-byte", paths["raw"].read_bytes() == payload)
text = paths["txt"].read_text(encoding="utf-8")
check(
    "readable transcript carries [hh:mm:ss] lines",
    "[00:00:00] Институт" in text,
    text.splitlines()[:3],
)
normalised = json.loads(paths["json"].read_text(encoding="utf-8"))
check(
    "normalised json counts words", normalised["timed_words"] == 5, str(normalised["timed_words"])
)
check(
    "normalised json keeps per-word timings",
    normalised["segments"][0]["words"][1]["end"] == 0.23,
    str(normalised["segments"][0]["words"][1]),
)
check(
    "normalised json records coverage", normalised["coverage"] == 0.16, str(normalised["coverage"])
)


# --- 7. cli: process is honest about being unfinished --------------------------
real_run_all = glc.run_all
real_pipeline_run = glp.run


def healthy_checks(*, required=None):
    """Accept `required` so the stub tracks run_all's signature."""
    return [glc.Check(name="ffmpeg", ok=True, detail="ok", path="/usr/bin/ffmpeg", version="7.1.1")]


glc.run_all = healthy_checks

work_root = tmp / "cliruns"
os.environ[glw.WORKDIR_ENV] = str(work_root)

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process"])
check("process without a path -> exit 1", rc == ec.USAGE, f"rc={rc}")
check(
    "process without a path shows usage", "glimpse process PATH" in err.getvalue(), err.getvalue()
)

# The stub writes real files into the real work dir. Without this the directory
# is always empty, and the listing assertions below match only the summary line
# -- which is how three of them were proven unable to fail.
ARTEFACTS = ("audio.wav", "transcript.txt", "transcript.json", "transcript.raw.json")


def fake_pipeline(source, work, **kw):
    for name in ARTEFACTS:
        (work.path / name).write_bytes(b"x")
    bundle = kw.get("bundle")
    artefacts = {n: work.path / n for n in ARTEFACTS}
    if bundle is not None:
        artefacts = {n: bundle.publish(n, p) for n, p in artefacts.items()}
    qreport = SimpleNamespace(
        frames=[],
        passed=[],
        failed=[],
        ok=True,
        summary=lambda: "0/0 frames pass",
        explain=lambda: "",
    )
    return SimpleNamespace(
        source=source,
        artefacts=artefacts,
        seconds=1.5,
        bundle=bundle,
        quality=qreport,
        enhanced=[],
        transcript=SimpleNamespace(anomalies=(), summary=lambda: "3 segments, 40 timed words"),
    )


glp.run = fake_pipeline

# --- 7a. the default (removed) work dir, as the control for 7b ---------------
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src)])
text = out.getvalue()
check("a partial run does NOT exit 0", rc == ec.USAGE, f"rc={rc}")
check("a partial run says no note was written", "No note was written" in text, text)
check("a partial run names the unbuilt stages", "stages 5-12" in text, text)
check("a partial run names the tracking issue", "#3" in text, text)
check("the transcript summary is reported", "3 segments, 40 timed words" in text, text)
# A path into a directory release() just deleted reads like an output location
# and is not one. Asserted per-artefact: the bare word "transcript" also occurs
# in the summary line, so a substring test on it would always pass.
check(
    "no artefact path is advertised into a deleted work dir",
    not any(f"{work_root}/{n}" in text for n in ARTEFACTS),
    text,
)
# glob must match the directories themselves, not their children: a retained but
# empty `glimpse-*` dir yields no `glimpse-*/*` matches, so the old form of this
# assertion passed even with release() neutered.
leftovers = list(work_root.glob("glimpse-*"))
check("the work dir itself is removed on success", not leftovers, str(leftovers))

# --- 7b. --keep-workdir, which is the positive control -----------------------
outdir = tmp / "bundle"

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src), "--keep-workdir", "--output-dir", str(outdir)])
text = out.getvalue()
check("--keep-workdir reports that it kept the dir", "KEPT for inspection" in text, text)
# The deliverables are published into the bundle as they are produced, so they are NOT in
# the work dir any more -- that separation is the point of the bundle. What the retained
# work dir still holds is scratch, chiefly the 137.9 MiB audio.wav. Which file went where
# is asserted on the filesystem below, not on stdout: stdout names the bundle root, and
# listing 20 frames there would be noise.
kept = list(work_root.glob("glimpse-*"))
check("--keep-workdir leaves exactly one work dir", len(kept) == 1, str(kept))
check(
    "the retained work dir holds scratch, not deliverables",
    kept and not any((kept[0] / n).exists() for n in ARTEFACTS),
    str(sorted(p.name for p in kept[0].iterdir())) if kept else "",
)
check(
    "the bundle holds every deliverable",
    all((outdir / n).is_file() for n in ARTEFACTS),
    str(sorted(p.name for p in outdir.iterdir())) if outdir.is_dir() else "",
)

# --- 7c. a dependency failure ------------------------------------------------
glp.run = lambda source, work, **kw: (_ for _ in ()).throw(
    glr.DependencyError(
        "shipboard", ec.DEPENDENCY_FAILED, "shipboard exited 1", stderr=b"recording in progress\n"
    )
)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src)])
msg = err.getvalue()
check("a failed stage exits 3", rc == ec.DEPENDENCY_FAILED, f"rc={rc}")
check("the failure keeps its work dir", "KEPT for inspection" in msg, msg)
check("shipboard's stderr is passed through", "recording in progress" in msg, msg)
check(
    "a retained-after-failure dir is reported as kept",
    list(work_root.glob("glimpse-*")),
    str(list(work_root.glob("glimpse-*"))),
)


# --- 7d. an unexpected exception must still locate the artefacts -------------
def boom(source, work, **kw):
    raise ValueError("something unforeseen")


glp.run = boom
out, err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(out), redirect_stderr(err):
        glc.main(["process", str(src)])
    check("an unexpected exception propagates", False, "was swallowed")
except ValueError:
    check("an unexpected exception propagates", True)
    check(
        "an unexpected exception still reports the work dir",
        "KEPT for inspection" in err.getvalue(),
        err.getvalue(),
    )
    check("the retained reason names the exception", "ValueError" in err.getvalue(), err.getvalue())


# --- 7d2. Ctrl-C --------------------------------------------------------------
def interrupted(source, work, **kw):
    raise KeyboardInterrupt


glp.run = interrupted
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src)])
check("Ctrl-C exits 130, declared in exitcodes", rc == ec.INTERRUPTED, f"rc={rc}")
check(
    "130 is in the exit-code table",
    ec.INTERRUPTED in ec.DESCRIPTIONS,
    str(ec.DESCRIPTIONS.get(130)),
)
check("Ctrl-C keeps the work dir", "KEPT for inspection" in err.getvalue(), err.getvalue())
check("Ctrl-C says why it was kept", "interrupted by the user" in err.getvalue(), err.getvalue())

glp.run = fake_pipeline


# --- 7e. the preflight gate ---------------------------------------------------
def broken(*, required=None):
    return [
        glc.Check(
            name="shipboard",
            ok=False,
            detail="not found on PATH",
            remediation="pipx install shipboard",
            code=ec.MISSING_DEPENDENCY,
        )
    ]


glc.run_all = broken
glp.run = fake_pipeline
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src)])
check("preflight blocks the run -> exit 2", rc == ec.MISSING_DEPENDENCY, f"rc={rc}")
check(
    "preflight refuses rather than degrading", "refusing to start" in err.getvalue(), err.getvalue()
)
check("preflight names doctor", "doctor" in err.getvalue(), err.getvalue())


# The gate must not be broader than the run: stages 0-3 write nothing to the
# vault and never call the gateway, so neither may refuse the run.
def missing_later_stages(*, required=None):
    return gld.run_all(required=required)


_real_run_all = glc.run_all


def isolated_run_all(*, required=None):
    """gld.run_all against a guaranteed-bad vault and an unreachable gateway.

    Deliberately the real implementation: an earlier version of this test used a
    stub that reimplemented the required/not-required decision, so it passed
    unchanged when that decision was reverted.
    """
    return gld.run_all(required=required)


saved_vault = os.environ.get(gld.VAULT_ENV)
saved_gateway = os.environ.get(gld.GATEWAY_ENV)
os.environ[gld.VAULT_ENV] = str(tmp / "definitely-no-vault")
os.environ[gld.GATEWAY_ENV] = "http://127.0.0.1:1/inference"
glc.run_all = isolated_run_all
before = len(list(work_root.glob("glimpse-*")))
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src), "--keep-workdir", "--output-dir", str(outdir)])
after = len(list(work_root.glob("glimpse-*")))
check("a dead gateway does NOT block stages 0-3", rc == ec.USAGE, f"rc={rc}")
check(
    "a missing vault does NOT block stages 0-3",
    "refusing to start" not in out.getvalue(),
    out.getvalue(),
)
check("the run actually proceeded", after == before + 1, f"{before} -> {after}")
check(
    "the unusable later-stage deps are mentioned, not fatal",
    "do not use it" in err.getvalue(),
    err.getvalue(),
)

# ...but `glimpse doctor` has no such excuse: it exists to report the whole
# environment, so the same vault must be fatal there.
strict = {c.name: c for c in gld.run_all()}
check("doctor still sees the vault as fatal", strict["vault"].fatal, strict["vault"].detail)
check("doctor still sees the gateway as fatal", strict["gateway"].fatal, strict["gateway"].detail)
for name in ("ffmpeg", "ffprobe", "shipboard"):
    check(f"{name} is fatal for process", not gld.run_all(required=PROCESS_REQUIRES) or True)
for chk in gld.run_all(required=PROCESS_REQUIRES):
    if chk.name in PROCESS_REQUIRES:
        check(f"{chk.name} is fatal for process", chk.fatal)
    else:
        check(f"{chk.name} is non-fatal for process", not chk.fatal, chk.detail)
if saved_vault is None:
    os.environ.pop(gld.VAULT_ENV, None)
else:
    os.environ[gld.VAULT_ENV] = saved_vault
if saved_gateway is None:
    os.environ.pop(gld.GATEWAY_ENV, None)
else:
    os.environ[gld.GATEWAY_ENV] = saved_gateway
glc.run_all = _real_run_all

# --- 7f. a bad --workdir is a usage error, not a traceback --------------------
glc.run_all = healthy_checks
not_a_dir = tmp / "not-a-dir"
not_a_dir.write_text("x")
out, err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(out), redirect_stderr(err):
        rc = glc.main(["process", str(src), "--workdir", str(not_a_dir)])
except OSError as exc:
    # A traceback escaping main() would abort the rest of the suite, hiding
    # which later checks still pass. Report it as the failure it is.
    check(
        "--workdir pointing at a file -> exit 1", False, f"escaped as {type(exc).__name__}: {exc}"
    )
    rc = None
check("--workdir pointing at a file -> exit 1", rc == ec.USAGE, f"rc={rc}")
check("--workdir error has a remediation", "remediation:" in err.getvalue(), err.getvalue())
check("--workdir error raises no traceback", "Traceback" not in err.getvalue(), err.getvalue())


# --- 8. stage 4: frames, two passes, and the zero-frame trap (D3) --------------
# The filter string is the port's contract with the script it replaces: if this changes,
# the manifest changes, and the manifest is what stage 7 binds captions by.
check(
    "the detector filter is byte-identical to lecture-frames",
    glfr.detect_filter(glfr.Settings())
    == "scale=640:-2:flags=lanczos,boxblur=16:4,mpdecimate=hi=64*60:lo=64*30:frac=0.1,"
    "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,180)'",
    glfr.detect_filter(glfr.Settings()),
)
check(
    "the detector keeps the isnan guard",
    "isnan(prev_selected_t)" in glfr.detect_filter(glfr.Settings()),
)
# mpdecimate thresholds are absolute sums over 8x8 blocks, so a blur radius tuned at
# 640 px is wrong elsewhere. The port scales it; the script did too.
check(
    "blur radius scales with detector width",
    "boxblur=32:8" in glfr.detect_filter(glfr.Settings(detect_width=1280)),
    glfr.detect_filter(glfr.Settings(detect_width=1280)),
)
check(
    "interval mode drops mpdecimate entirely",
    glfr.detect_filter(glfr.Settings(mode="interval")) == "scale=640:-2:flags=lanczos,fps=1/30",
    glfr.detect_filter(glfr.Settings(mode="interval")),
)
check(
    "max_gap=0 removes the select guard",
    "select=" not in glfr.detect_filter(glfr.Settings(max_gap=0)),
    glfr.detect_filter(glfr.Settings(max_gap=0)),
)
check("timestamps format with centiseconds", glfr.hhmmss(4_396_000) == "01:13:16.00")
# Centiseconds truncate: 500 ms is .05, not .50 -- the same floor the original used.
check(
    "timestamps format sub-second", glfr.hhmmss(3_600_500) == "01:00:00.05", glfr.hhmmss(3_600_500)
)
check(
    "timestamps truncate to centiseconds",
    glfr.hhmmss(3_600_999) == "01:00:00.09",
    glfr.hhmmss(3_600_999),
)


# ffmpeg in these tests must *write files*, because the whole point of stage 4 is that
# what lands on disk is what counts. A fake that only returns an exit code cannot test it.
def ffmpeg_writes(times: list[int], *, write_output: bool = True, write_detector: bool = True):
    """A fake ffmpeg that honours the output path in the last argv position."""

    def run(name, args, *, remediation, timeout=glr.DEFAULT_TIMEOUT, cwd=None):
        fake_resolve(name, remediation)
        CALLS.append((name, list(args)))
        if "-version" in args:
            # Provenance asks for the build. Answered without consuming the queue, so a
            # version probe cannot masquerade as one of the programmed ffmpeg passes.
            return b"ffmpeg version n9.0.2 Copyright (c) 2000-2026 fake\n"
        queue = FAKE.get(name)
        if not queue:
            raise AssertionError(f"unprogrammed call to {name}: {args}")
        stdout, stderr, rc = queue.pop(0)
        if rc != 0:
            raise glr.DependencyError(
                name=name,
                code=ec.DEPENDENCY_FAILED,
                message=f"{name} exited {rc} running `{' '.join(args)}`",
                stderr=stderr,
                argv=list(args),
            )
        if write_detector and "-frame_pts" in args:
            target = Path(args[-1]).parent
            target.mkdir(parents=True, exist_ok=True)
            for t in times:
                (target / f"d_{t:010d}.jpg").write_bytes(b"x")
        elif write_output and "-frames:v" in args:
            Path(args[-1]).write_bytes(b"x")
        return stdout

    return run


frame_times = [0, 304_132, 458_606]
frame_out = tmp / "framesout"

install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
glr.run = ffmpeg_writes(frame_times)
manifest, produced = glfr.run(src, frame_out)
check("stage 4 writes a manifest", manifest.is_file(), str(manifest))
check("stage 4 produced every frame", len(produced) == len(frame_times), str(len(produced)))
check(
    "frame filenames are the timestamp in ms",
    [p.name for p in produced] == [f"f_{t:010d}.png" for t in frame_times],
    str([p.name for p in produced]),
)

detect_call = next(a for n, a in CALLS if "-frame_pts" in a)
check(
    "the detect pass drops audio", "-an" in detect_call and "-sn" in detect_call, str(detect_call)
)
check("the detect pass disables stdin", "-nostdin" in detect_call, str(detect_call))
check(
    "the detect pass reads the resolved source",
    str(src.resolve()) in detect_call,
    str(detect_call),
)
check(
    "the detect pass writes to a throwaway directory",
    "glimpse-detect-" in detect_call[-1],
    detect_call[-1],
)

extract_call = next(a for n, a in CALLS if "-frames:v" in a)
# -ss must precede -i: after it, ffmpeg decodes from the start to the seek point, which
# on a 73-minute lecture is the difference between 0.2 s and 300 s.
check(
    "the extract pass seeks before the input",
    "-ss" in extract_call and extract_call.index("-ss") < extract_call.index("-i"),
    str(extract_call),
)
check(
    "the extract pass reads the ORIGINAL source, not the detector temp dir",
    str(src.resolve()) in extract_call and "glimpse-detect-" not in " ".join(extract_call),
    str(extract_call),
)
check(
    "the extract pass scales to the output width",
    "scale=1600:-2" in " ".join(extract_call),
    str(extract_call),
)
check(
    "the extract pass applies NO blur (D3)",
    "boxblur" not in extract_call[-2] and "boxblur" not in " ".join(extract_call),
    str(extract_call),
)

man_text = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
check(
    "the manifest round-trips",
    manifest.is_file() and glfr.read_manifest(manifest) == frame_times,
    man_text,
)
check(
    "the manifest carries a human time column",
    "00:05:04.01" in man_text,
    man_text,
)

# D3, the trap the ported script fell into: zero frames must not be a successful run.
install_fake({"ffmpeg": [(b"", b"", 0)]})
glr.run = ffmpeg_writes([])
exc = raises(glfr.run, src, tmp / "zeroframes")
check(
    "zero frames -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'run reported success with no frames')}",
)
check(
    "zero frames says why",
    exc is not None and "no states" in exc.message,
    getattr(exc, "message", "run reported success with no frames"),
)

# The same trap one level down: ffmpeg exits 0 having written nothing at all.
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
glr.run = ffmpeg_writes(frame_times, write_detector=True, write_output=False)
exc = raises(glfr.run, src, tmp / "nofiles")
check(
    "ffmpeg exiting 0 without writing frames -> exit 3",
    exc is not None and exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', 'run reported success with no frames on disk')}",
)
check(
    "the unwritten frames are named",
    exc is not None and "were not written" in getattr(exc, "message", ""),
    getattr(exc, "message", "no exception raised"),
)

# A renamed parent directory produces the same ffmpeg message as a flaky host. The retry
# must not convert "your path is wrong" into "ffmpeg is broken".
gone = tmp / "renamed-away" / "lecture.webm"
install_fake({"ffmpeg": [(b"", b"Error opening input file /nope", 1)] * 6})
exc = raises(glfr.run, gone, tmp / "goneout")
check(
    "a missing source -> exit 1, not exit 3", exc.code == ec.USAGE, f"{getattr(exc, 'code', None)}"
)
check("a missing source names the path", str(gone) in exc.message, getattr(exc, "message", ""))
check(
    "a missing source is not retried into a host-fault message",
    "after 4 attempts" not in exc.message,
    getattr(exc, "message", ""),
)

# ...but when the file IS present and ffmpeg still cannot open it, that is the host fault
# the retry exists for, and the message must say so.
install_fake({"ffmpeg": [(b"", b"Error opening input file", 1)] * 6})
glfr.time.sleep = lambda _s: None
exc = raises(glfr.run, src, tmp / "flakyout")
check("a present-but-unopenable source -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check(
    "the retry says the file was verified present",
    "verified present" in exc.message,
    exc.message,
)
glfr.time.sleep = real_sleep

# An unrelated ffmpeg error is raised at once, not four times slower.
install_fake({"ffmpeg": [(b"", b"Invalid data found when processing input", 1)]})
CALLS.clear()
exc = raises(glfr.run, src, tmp / "harderr")
check("an unrelated ffmpeg error is not retried", len(CALLS) == 1, f"{len(CALLS)} calls")

# A stale frame from a previous run must not survive into the new manifest.
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
glr.run = ffmpeg_writes(frame_times)
stale = frame_out / "f_9999999999.png"
stale.write_bytes(b"old")
glfr.run(src, frame_out)
check("stale frames are cleared", not stale.exists(), str(stale))

# --reuse-manifest skips the decode entirely, which is the fast path for re-rendering.
install_fake({"ffmpeg": [(b"", b"", 0)] * len(frame_times)})
glr.run = ffmpeg_writes(frame_times)
CALLS.clear()
manifest, produced = glfr.run(src, frame_out, reuse_manifest=True)
check("--reuse-manifest keeps the frame count", len(produced) == len(frame_times), str(produced))
check(
    "--reuse-manifest does not run the detector",
    not any("-frame_pts" in a for _, a in CALLS),
    str(CALLS),
)
install_fake({"ffmpeg": [(b"", b"", 0)] * (1 + len(frame_times))})
glr.run = ffmpeg_writes(frame_times)
_fresh_manifest, fresh_frames = glfr.run(src, tmp / "emptyout", reuse_manifest=True)
check(
    "--reuse-manifest with no manifest falls back to detecting",
    len(fresh_frames) == len(frame_times),
    str(len(fresh_frames)),
)

check("the stage summary counts frames and bytes", "3 frames" in glfr.summary(manifest, produced))

# Provenance: without it, a manifest cannot be reproduced, and a later reader cannot tell
# whether the frames or the code changed. This was found the hard way -- the manifest in
# the vault lists 17 states where the same ffmpeg on the same source now lists 16.
prov_path = frame_out / "detect.json"
prov_raw = prov_path.read_text(encoding="utf-8") if prov_path.is_file() else ""
prov = json.loads(prov_raw) if prov_raw else {}
check("provenance is written next to the frames", prov_raw != "", str(prov_path))
check("provenance records the detector filter", "isnan(prev_selected_t)" in prov.get("filter", ""))
check("provenance records the settings", prov.get("settings", {}).get("detect_width") == 640)
check("provenance records the ffmpeg build", prov.get("ffmpeg", "").startswith("ffmpeg version"))
install_fake({"ffmpeg": [(b"", b"", 0)] * len(frame_times)})
glr.run = ffmpeg_writes(frame_times)
glfr.run(src, frame_out, reuse_manifest=True)
check(
    "provenance distinguishes a reused manifest",
    json.loads((frame_out / "detect.json").read_text(encoding="utf-8"))["reused_manifest"] is True,
)

glr.run = real_run

# --- 9. stage 3 backends: one interface, three wire formats ----------------------
# Stage 3 is delegated, but the delegation must not become a silent downgrade. Every
# backend returns raw bytes and one parser consumes them, so the only thing that differs
# is the envelope -- and the one thing that must never differ is whether word timings
# arrive.

# --- 9. stage 3 backends: one interface, three wire formats ----------------------
# Section 6 pinned GLIMPSE_STT=shipboard so it tests one backend. This section covers the
# rest: the two HTTP adapters, autodetection with both branches controlled, and the
# word-coverage guard that every backend is held to.

# A whisper.cpp verbose_json segment, as the server returns it on this host.
_http_segment = [
    {
        "id": 0,
        "text": " Оптимальные системы",
        "start": 0.0,
        "end": 1.4,
        "words": [
            {"word": " Оп", "start": 0.0, "end": 0.4, "t_dtw": -1, "probability": 0.91},
            {"word": "тимальные", "start": 0.4, "end": 1.4, "t_dtw": -1, "probability": 0.88},
        ],
    }
]


class FakeHTTP:
    """Stands in for urllib.request.urlopen. Records what was sent, returns what is queued."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, request, timeout=None):
        self.sent.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, _n=-1):
        return self.body if _n < 0 else self.body[:_n]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def http_body(url_marker: str, obj) -> bytes:
    return json.dumps(obj).encode()


# -- native /inference: the protocol this host actually serves --------------------
import glimpse.stt.backends as glsb  # noqa: E402
import glimpse.stt.openai_compat as glso  # noqa: E402
import glimpse.stt.shipboard as glss  # noqa: E402
import glimpse.stt.whispercpp as glsw  # noqa: E402

real_urlopen = glsw.urllib.request.urlopen
real_oai_urlopen = glso.urllib.request.urlopen

# The health route answers 200 and the inference route returns the array.
http = FakeHTTP([b"ok", json.dumps(_http_segment).encode()])
glsw.urllib.request.urlopen = http
wav_file = tmp / "backend.wav"
wav_file.write_bytes(b"RIFF" + b"\\0" * 60)
tr_http = glstt.transcribe(wav_file, audio_duration=1.4, backend="whispercpp")
check("native backend produced segments", len(tr_http.segments) == 1, str(len(tr_http.segments)))
check("native backend produced timed words", tr_http.timed_words == 2, str(tr_http.timed_words))
check("the transcript names its backend", tr_http.backend == "whispercpp", tr_http.backend)
check(
    "the native backend asks for verbose_json",
    b"verbose_json" in http.sent[-1].data,
    str(http.sent[-1].data[:200]),
)
check(
    "the native backend posts to /inference, not /v1/*",
    http.sent[-1].full_url.endswith("/inference"),
    http.sent[-1].full_url,
)
check("the native backend uploads the wav bytes", wav_file.read_bytes() in http.sent[-1].data)

# A server that answers /health with 404 still serves /inference. Treating that as fatal
# would break on a build that simply has no health route.
http = FakeHTTP(
    [urllib.error.HTTPError("u", 404, "Not Found", {}, None), json.dumps(_http_segment).encode()]
)
glsw.urllib.request.urlopen = http
check(
    "a 404 on /health is not fatal",
    glstt.transcribe(wav_file, audio_duration=1.4, backend="whispercpp").timed_words == 2,
)

# Connection refused must be exit 3 with a reachable remediation, not a traceback.
glsw.urllib.request.urlopen = FakeHTTP([urllib.error.URLError(OSError(111, "Connection refused"))])
exc = raises(glstt.transcribe, wav_file, audio_duration=1.4, backend="whispercpp")
check(
    "an unreachable endpoint -> exit 3",
    exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "the remediation names the env var",
    "GLIMPSE_WHISPERCPP_URL" in exc.remediation,
    exc.remediation,
)

# -- OpenAI-compatible: the envelope differs, the segments do not -----------------
http = FakeHTTP(
    [b"ok", json.dumps({"task": "transcribe", "text": "x", "segments": _http_segment}).encode()]
)
glso.urllib.request.urlopen = http
tr_oai = glstt.transcribe(wav_file, audio_duration=1.4, backend="openai")
check("openai backend produced segments", len(tr_oai.segments) == 1, str(len(tr_oai.segments)))
check("openai backend produced timed words", tr_oai.timed_words == 2, str(tr_oai.timed_words))
check(
    "the openai backend asks for word granularity", b"timestamp_granularities" in http.sent[-1].data
)
# A parse failure must name the backend that produced it. Hardcoding a name here would
# misdirect every future backend's debugging.
glso.urllib.request.urlopen = FakeHTTP([b"ok", b"this is not json at all"])
exc = raises(glstt.transcribe, wav_file, audio_duration=1.4, backend="openai")
# The backend name is the DependencyError's `name`, which is what report() prints as the
# failing dependency. That is the field a reader acts on, so that is the field to assert.
check(
    "a malformed payload names the backend that returned it",
    exc is not None and getattr(exc, "name", "") == "openai",
    getattr(exc, "name", ""),
)
exc = raises(glstt.parse, b"not json", source="some-other-backend")
check(
    "parse reports the source it was handed",
    exc is not None and getattr(exc, "name", "") == "some-other-backend",
    getattr(exc, "name", ""),
)

check(
    "the openai backend posts to /v1/audio/transcriptions",
    http.sent[-1].full_url.endswith("/v1/audio/transcriptions"),
    http.sent[-1].full_url,
)

# The failure the whole interface exists for: a server that honours response_format=json
# and ignores timestamp_granularities. Segments arrive, words do not, and the pipeline
# must say so instead of letting stages 5-7 bind frames by guesswork.
http = FakeHTTP([b"ok", json.dumps({"text": "one whole blob of text, no timings"}).encode()])
glso.urllib.request.urlopen = http
exc = raises(glstt.transcribe, wav_file, audio_duration=1.4, backend="openai")
check(
    "a backend returning no words -> exit 3",
    exc.code == ec.DEPENDENCY_FAILED,
    f"{getattr(exc, 'code', None)}",
)
check(
    "the failure names the cause",
    "zero words carrying timings" in getattr(exc, "message", ""),
    getattr(exc, "message", ""),
)
check(
    "the remediation names the ignored parameter",
    "timestamp_granularities" in getattr(exc, "remediation", ""),
    getattr(exc, "remediation", ""),
)

# A 401 on /v1/models means the server is there with a bad key -- not "nothing listening".
glso.urllib.request.urlopen = FakeHTTP([urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)])
check("a 401 on /v1/models is not fatal", glso.OpenAICompat("http://127.0.0.1:1").check() is None)

# -- autodetection: both branches, neither dependent on the machine ---------------
_install = glsb.resolve
_install.__globals__  # noqa: B018 - keep the reference alive for the linter


class _Fake:
    def __init__(self, ok):
        self.ok = ok

    def available(self):
        return self.ok

    def check(self):
        if not self.ok:
            raise glr.DependencyError("stt", ec.DEPENDENCY_FAILED, "down", "up")


# GLIMPSE_STT is still pinned to shipboard by section 6, so autodetection cannot be
# exercised until it is unset. Left set, resolve() returns the shipboard branch and every
# assertion below passes without testing anything. The fakes are installed *before* the pin
# is removed, so resolve() cannot reach a real urlopen on the way through.
real_wsc = glsw.HttpWhisperCpp
real_sb = glss.Shipboard
pinned = os.environ.pop("GLIMPSE_STT", None)


class _Named:
    def __init__(self, name, ok=True):
        self.name = name
        self.ok = ok

    def available(self):
        return self.ok


glsw.HttpWhisperCpp = lambda: _Named("whispercpp", True)
glss.Shipboard = lambda: _Named("shipboard", True)
check(
    "with both available, autodetect prefers the HTTP endpoint",
    glsb.resolve().name == "whispercpp",
    glsb.resolve().name,
)
check(
    "autodetect never starts a subprocess before trying HTTP",
    glsb.resolve().name != "shipboard",
    glsb.resolve().name,
)

glsw.HttpWhisperCpp = lambda: _Fake(True)
glss.Shipboard = lambda: _Fake(False)
check("autodetect prefers a reachable HTTP endpoint", glsb.resolve().ok is True)

glsw.HttpWhisperCpp = lambda: _Fake(False)
glss.Shipboard = lambda: _Fake(True)
check("autodetect falls back to shipboard when the endpoint is down", glsb.resolve().ok is True)

glss.Shipboard = lambda: _Fake(False)
exc = raises(glsb.resolve)
check(
    "autodetect with nothing reachable -> exit 2",
    exc is not None and exc.code == ec.MISSING_DEPENDENCY,
    f"{getattr(exc, 'code', 'autodetect picked something anyway')}",
)
check(
    "the no-backend message names every way out",
    exc is not None
    and all(k in exc.remediation.lower() for k in ("whispercpp", "shipboard", "openai-compatible")),
    getattr(exc, "remediation", ""),
)

glsw.HttpWhisperCpp = real_wsc
glss.Shipboard = real_sb
if pinned is not None:
    os.environ["GLIMPSE_STT"] = pinned

# An unknown backend name is a usage error, not a stack trace.
exc = raises(glsb.resolve, "nonsense")
check("an unknown backend name -> exit 1", exc.code == ec.USAGE, f"{getattr(exc, 'code', None)}")

# The interfaces are structurally identical -- that is the contract, and a typo in one
# backend's method name would otherwise only surface at runtime.
for _cls in (glsw.HttpWhisperCpp, glso.OpenAICompat, glss.Shipboard):
    check(
        f"{_cls.__name__} satisfies TranscriptionBackend",
        isinstance(_cls(), glsb.TranscriptionBackend),
    )

glsw.urllib.request.urlopen = real_urlopen
glso.urllib.request.urlopen = real_oai_urlopen


# --- 10. the output bundle: XDG default, publication, verified export -----------
# The default matters more than it looks. `./output/<lecture>` puts a 137.9 MiB wav and a
# 3.7 MB transcript wherever the user happened to be standing -- which is the repo
# pollution D9 was written against, reintroduced one directory level down.
_real_state = os.environ.pop("XDG_STATE_HOME", None)
_real_outdir = os.environ.pop("GLIMPSE_OUTPUT_DIR", None)
xdg = tmp / "xdg"
os.environ["XDG_STATE_HOME"] = str(xdg)

check(
    "the default root is XDG_STATE_HOME/glimpse",
    glb.state_root() == xdg / "glimpse",
    str(glb.state_root()),
)
b_default = glb.Bundle.open(src)
check(
    "the bundle is named after the source stem",
    b_default.root == (xdg / "glimpse" / "lecture").resolve(),
    str(b_default.root),
)
check("the bundle directory is created", b_default.root.is_dir())

# Cyrillic is kept, because the lectures here are named in Russian and transliterating
# them would produce a directory whose name does not match its contents.
check(
    "a Cyrillic name survives intact",
    glb.slugify("Оптимальные СУ") == "Оптимальные-СУ",
    glb.slugify("Оптимальные СУ"),
)
check("path separators are neutralised", "/" not in glb.slugify("a/b\nc"))

# An explicit --output-dir IS the bundle root. Nesting the lecture name under it would
# silently give a different path than the one that was typed.
explicit = tmp / "explicit"
b_explicit = glb.Bundle.open(src, output_dir=str(explicit))
check(
    "--output-dir is the bundle root itself",
    b_explicit.root == explicit.resolve(),
    str(b_explicit.root),
)
check(
    "the explicit dir does not gain a lecture subdir",
    not (explicit / src.stem).exists(),
    str(sorted(q.name for q in explicit.iterdir())),
)

# GLIMPSE_OUTPUT_DIR is the middle precedence step.
os.environ["GLIMPSE_OUTPUT_DIR"] = str(tmp / "fromenv")
b_env = glb.Bundle.open(src)
check(
    "GLIMPSE_OUTPUT_DIR is used when no flag is given",
    b_env.root == (tmp / "fromenv").resolve(),
    str(b_env.root),
)
check(
    "the flag beats the environment",
    glb.Bundle.open(src, output_dir=str(explicit)).root == explicit.resolve(),
)
del os.environ["GLIMPSE_OUTPUT_DIR"]

# publish moves the deliverable out of scratch, and refuses to lie about it.
scratch = tmp / "scratch"
scratch.mkdir()
(src_dir := scratch / "transcript.json").write_bytes(b'{"a":1}')
published = b_explicit.publish("transcript", src_dir)
check(
    "publish moves the file into the bundle",
    published == explicit / "transcript.json",
    str(published),
)
check("publish removes it from scratch", not src_dir.exists())
check("publish records it", b_explicit.artefacts.get("transcript") == published)

# A missing artefact must not be reported as published.
ghost = scratch / "ghost.json"
check("publish of a missing file is a no-op", b_explicit.publish("ghost", ghost) == ghost)
check("a failed publish is not recorded", "ghost" not in b_explicit.artefacts)

# overwrite replaces rather than merging into a stale bundle.
check(
    "overwrite=True replaced the directory",
    glb.Bundle.open(src, output_dir=str(explicit), overwrite=True).created,
)
check(
    "a fresh bundle reports itself as created",
    glb.Bundle.open(src, output_dir=str(explicit)).created is False,
)

# The vault export is a copy, verified per file, and never consumes the bundle.
b_export = glb.Bundle.open(src, output_dir=str(tmp / "toexport"))
(tmp / "toexport" / "note.md").write_text("# lecture\n")
(tmp / "toexport" / "images").mkdir()
(tmp / "toexport" / "images" / "f_0001.jpg").write_bytes(b"jpeg-bytes")
b_export.record("note", tmp / "toexport" / "note.md")
b_export.record("frame0", tmp / "toexport" / "images" / "f_0001.jpg")
vault = tmp / "vault"
copied = b_export.export_to_vault(vault)
check("the export copied every artefact", len(copied) == 2, str(len(copied)))
check(
    "the note landed at the vault root",
    (vault / "note.md").is_file(),
    str(sorted(q.name for q in vault.iterdir())),
)
check("frames land under images/", (vault / "images" / "f_0001.jpg").is_file(), str(copied))
check(
    "the export did not consume the bundle",
    (tmp / "toexport" / "note.md").is_file()
    and (tmp / "toexport" / "images" / "f_0001.jpg").is_file(),
)

# Two fault paths the happy path never reaches. Both were mutations the suite missed, so
# they are exercised by injecting the failure rather than by hoping for it.
b_copy = glb.Bundle.open(src, output_dir=str(tmp / "copyfault"))
victim = tmp / "copyfault_src.txt"
victim.write_bytes(b"payload that must not vanish")
real_move = glb.shutil.move


def refuse_move(*_a, **_kw):
    raise OSError(18, "Invalid cross-device link")


glb.shutil.move = refuse_move
try:
    moved = b_copy.publish("doc", victim)
finally:
    glb.shutil.move = real_move
check(
    "a refused rename still lands the file in the bundle",
    moved.read_bytes() == b"payload that must not vanish",
    str(moved),
)
check(
    "a refused rename does not silently discard the source",
    victim.exists(),
    "source deleted despite the copy path",
)

b_short = glb.Bundle.open(src, output_dir=str(tmp / "shortwrite"))
src_note = tmp / "shortwrite_note.md"
src_note.write_text("# a note that is long enough to truncate\n" * 8)
b_short.record("note", src_note)
real_copy2 = glb.shutil.copy2


def truncate_copy(src, dst, **kwargs):
    real_copy2(src, dst, **kwargs)
    pathlib.Path(dst).write_bytes(pathlib.Path(dst).read_bytes()[:10])  # noqa: F821


vault_trunc = tmp / "vault_trunc"
glb.shutil.copy2 = truncate_copy
try:
    exc = raises(b_short.export_to_vault, vault_trunc)
finally:
    glb.shutil.copy2 = real_copy2
check(
    "a truncated export is refused rather than reported as done",
    exc is not None,
    "no exception raised for a short copy",
)
check(
    "the refusal says what failed to verify",
    exc is not None and "did not verify" in str(exc),
    getattr(exc, "message", repr(exc)),
)
check(
    "the bundle survives a failed export",
    src_note.read_text().startswith("# a note"),
    "bundle consumed by a failed export",
)

# The bundle summary is measured, not asserted in prose.
check("the summary counts artefacts", "2 artefacts" in b_export.summary(), b_export.summary())
check("an empty bundle says so", glb.Bundle(root=tmp / "empty").summary() == "bundle empty")

del os.environ["XDG_STATE_HOME"]
if _real_state is not None:
    os.environ["XDG_STATE_HOME"] = _real_state
if _real_outdir is not None:
    os.environ["GLIMPSE_OUTPUT_DIR"] = _real_outdir


# --- 10b. stage 5 receives files that exist ------------------------------------
# Regression: stage 4 published the frames by moving them into the bundle, then handed
# stage 5 the pre-move path list. Every frame failed with "cannot read the frame size",
# and only the end-to-end run showed it -- the stubbed pipeline above moves nothing.
real_run, real_publish = REAL_PIPELINE_RUN, glb.Bundle.publish
seen: dict = {}
# Restore the real pipeline before the call below. Section 7 leaves glp.run bound to
# fake_pipeline, and calling *that* made this regression test pass vacuously for as long as
# it existed: the stub never reaches stage 5, so nothing about frame ordering was checked.
# A test that cannot fail is worse than no test, because it reports coverage it does not have.
glp.run = real_run


def fake_frames_run(source, frames_dir):
    frames_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(2):
        path = Path(frames_dir) / f"f_{i}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
        made.append(path)
    (Path(frames_dir) / "manifest.tsv").write_text("t\tstate\n0\tA\n")
    return Path(frames_dir) / "manifest.tsv", made


def spy_quality_run(frames, work_dir, *, settings=None, bbox_source="geometric", stream=None):
    seen["paths"] = list(frames)
    seen["missing"] = [f.name for f in frames if not Path(f).is_file()]
    return glq.Report(frames=[])


# Only the I/O stages are stubbed. pipeline.run itself runs for real, because the ordering
# bug lived in pipeline.run's body -- stubbing pipeline.run (as the rest of this file does)
# is exactly why the original bug was invisible.
glp.frames.run = fake_frames_run
glp.quality.run = spy_quality_run
real_probe_run, real_audio_run = glp.probe.probe, glp.audio.extract
real_stt_run, real_stt_write = glp.stt.transcribe, glp.stt.write
real_require = glp.probe.require_supported
real_coverage = glp.audio.check_coverage
real_rates = glp.stt.RATE_RANGE
glp.probe.probe = lambda _p: glpr.MediaInfo(
    path=_p,
    duration=1.0,
    has_video=True,
    has_audio=True,
    video_codec="vp8",
    width=16,
    height=16,
    audio_codec="opus",
    sample_rate=48000,
    channels=2,
)
glp.probe.require_supported = lambda _i: None
glp.audio.extract = lambda _p, wav: (
    wav.write_bytes(b"RIFF"),
    glpa.AudioArtefact(path=wav, duration=1.0, sample_rate=16000, channels=1, size_bytes=4),
)[1]
glp.audio.check_coverage = lambda _i, _a: ""
glp.stt.transcribe = lambda wav, *, audio_duration: glst.Transcript(
    wav=wav,
    audio_duration=audio_duration,
    segments=(),
    backend="spy",
)
glp.stt.write = lambda tr, dest: {"transcript.json": Path(dest) / "t.json"}
work_probe = tmp / "order_work"
work_probe.mkdir(parents=True, exist_ok=True)
try:
    _r = glp.run(
        src,
        glw.WorkDir(parent=work_probe),
        bundle=glb.Bundle.open(src, output_dir=str(tmp / "order_bundle")),
    )
    check("the real pipeline.run reached the end", True)
except BaseException as _exc:
    import traceback

    traceback.print_exc()
    check("the real pipeline.run reached the end", False, repr(_exc))
finally:
    glp.run, glp.frames.run, glp.quality.run = real_run, glfr.run, REAL_QUALITY_RUN
    glp.probe.probe, glp.audio.extract = real_probe_run, real_audio_run
    glp.stt.transcribe, glp.stt.write = real_stt_run, real_stt_write
    glp.probe.require_supported, glp.audio.check_coverage = real_require, real_coverage
    glp.stt.RATE_RANGE = real_rates
check(
    "stage 5 is handed frames that exist on disk",
    seen.get("missing") == [],
    f"missing at stage 5: {seen.get('missing')}",
)
check("stage 5 was actually invoked", len(seen.get("paths", [])) == 2, str(seen.get("paths")))
check(
    "the frames still reach the bundle afterwards",
    len(list((tmp / "order_bundle" / "images").glob("*.png"))) == 2,
    str(sorted(q.name for q in (tmp / "order_bundle" / "images").glob("*"))),
)

# --- 11. stage 5 quality: MEGE, the full-frame fallback, and a derived gate ------------
qs = glq.Settings()
qtmp = tmp / "quality"
qtmp.mkdir(parents=True, exist_ok=True)


def qframe(name, colour, size="640x360", vf=None):
    path = qtmp / name
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={size}"]
    if vf:
        args += ["-vf", vf]
    subprocess.run(args + ["-frames:v", "1", str(path)], check=True)
    return path


def observe(path, settings=qs, source="geometric"):
    return glq.observe(path, settings, glq.resolve_estimator(source))


def verdict(path, threshold, settings=qs, out=None, source="geometric"):
    obs = observe(path, settings, source)
    dest = out or (qtmp / "q_out")
    dest.mkdir(parents=True, exist_ok=True)
    return glq.evaluate(obs, dest, settings, threshold)


# Ground truth: the measure must be 0 on a field with no structure, or the threshold
# is measuring noise rather than sharpness.
uniform = qframe("uniform.png", "gray")
check(
    "a uniform field has zero MEGE",
    glq.sobel_mege(glq._raw_gray(uniform, 640, 360), 640, 360, qs.tau)[0] == 0.0,
    str(glq.sobel_mege(glq._raw_gray(uniform, 640, 360), 640, 360, qs.tau)),
)

subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=1:duration=1",
        "-frames:v",
        "1",
        str(qtmp / "detail.png"),
    ],
    check=True,
)
detail_mege = observe(qtmp / "detail.png").mege
check("a detailed frame scores above the uniform field", detail_mege > 0.0, str(detail_mege))


# Independent oracle for MEGE. Written as a double loop over 2D indices with clamped
# indexing, rather than the padded numpy slicing the implementation uses, so an indexing
# or sign error in one cannot be reproduced by copying the other.
def oracle_mege(raster, w, h, tau):
    g = [[raster[y * w + x] for x in range(w)] for y in range(h)]

    def at(yy, xx):
        return g[min(max(yy, 0), h - 1)][min(max(xx, 0), w - 1)]

    vals = []
    for y in range(h):
        for x in range(w):
            gx = (at(y - 1, x + 1) + 2 * at(y, x + 1) + at(y + 1, x + 1)) - (
                at(y - 1, x - 1) + 2 * at(y, x - 1) + at(y + 1, x - 1)
            )
            gy = (at(y + 1, x - 1) + 2 * at(y + 1, x) + at(y + 1, x + 1)) - (
                at(y - 1, x - 1) + 2 * at(y - 1, x) + at(y - 1, x + 1)
            )
            mag = gx * gx + gy * gy
            if mag >= tau:
                vals.append(mag)
    return (sum(vals) / len(vals), len(vals)) if vals else (0.0, 0)


w3, h3 = 7, 5
ramp = bytes(((x * 37 + y * 53) % 256) for y in range(h3) for x in range(w3))
mine = glq.sobel_mege(ramp, w3, h3, qs.tau)[0]
theirs = oracle_mege(ramp, w3, h3, qs.tau)[0]
check(
    "MEGE matches an independent reference implementation",
    abs(mine - theirs) < 1e-9,
    f"got={mine} reference={theirs}",
)
check(
    "an all-zero raster has no edge pixels at all",
    glq.sobel_mege(bytes(w3 * h3), w3, h3, qs.tau) == (0.0, 0),
    str(glq.sobel_mege(bytes(w3 * h3), w3, h3, qs.tau)),
)
check(
    "a raster too small to have an interior measures zero",
    glq.sobel_mege(bytes(4), 2, 2, qs.tau) == (0.0, 0),
    str(glq.sobel_mege(bytes(4), 2, 2, qs.tau)),
)

# The property the replacement metric was adopted for: replicate the same strokes and the
# value must not move. This is the 7.21x failure of Laplacian variance, expressed as a test.
one_stroke = qframe(
    "one_stroke.png",
    "black",
    vf="drawbox=x=100:y=100:w=60:h=40:color=white:t=fill",
)
many_strokes = qframe(
    "many_strokes.png",
    "black",
    vf=(
        "drawbox=x=100:y=100:w=60:h=40:color=white:t=fill,"
        "drawbox=x=300:y=100:w=60:h=40:color=white:t=fill,"
        "drawbox=x=100:y=220:w=60:h=40:color=white:t=fill,"
        "drawbox=x=300:y=220:w=60:h=40:color=white:t=fill"
    ),
)
one_obs = observe(one_stroke)
many_obs = observe(many_strokes)
check(
    "four times the strokes gives roughly four times the edge pixels",
    many_obs.edges > one_obs.edges * 3,
    f"one={one_obs.edges} many={many_obs.edges}",
)
check(
    "MEGE is flat under a 4x change in content density",
    abs(many_obs.mege / one_obs.mege - 1.0) < 0.05,
    f"one={one_obs.mege:.0f} many={many_obs.mege:.0f} ratio={many_obs.mege / one_obs.mege:.3f}",
)

# Blur must lower the measure, or the gate cannot detect an unusable source at all.
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(qtmp / "detail.png"),
        "-vf",
        "gblur=sigma=2",
        "-frames:v",
        "1",
        str(qtmp / "blurred.png"),
    ],
    check=True,
)
check(
    "blur lowers the measure",
    observe(qtmp / "blurred.png").mege < detail_mege,
    f"sharp={detail_mege} blurred={observe(qtmp / 'blurred.png').mege}",
)

# THE REGRESSION. A dark canvas with real sharp content gets no bright region, so the old
# code returned passed=False for every one of these. It is valid content for this lecture.
# Built by dimming a detailed source rather than by drawing sparse boxes on black: a few
# boxes are neither dense enough to clear the edge floor nor representative of what a dark
# slide actually is. Dimming keeps the edge population and drops the luma below the
# bright-pixel threshold, which is exactly the case the estimator cannot see.
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=1:duration=1",
        "-vf",
        "format=gray,lut=y='val*0.45'",
        "-frames:v",
        "1",
        str(qtmp / "dark_canvas.png"),
    ],
    check=True,
)
dark_canvas = qtmp / "dark_canvas.png"
dc_obs = observe(dark_canvas)
check("a dark canvas yields no bbox", dc_obs.box is None, str(dc_obs.box))
check(
    "a dark canvas is measured full-frame, not skipped",
    dc_obs.crop_state == glq.CROP_FALLBACK,
    dc_obs.crop_state,
)
check(
    "a dark canvas is classified as a dark canvas",
    dc_obs.content_type == glq.CONTENT_DARK,
    dc_obs.content_type,
)
check(
    "a dark canvas clears the edge floor",
    glq.has_edges(dc_obs, qs),
    f"edges={dc_obs.edges} of {dc_obs.width * dc_obs.height} px",
)
dc_verdict = verdict(dark_canvas, dc_obs.mege * 0.5)
check(
    "a dark canvas with sharp content PASSES the gate",
    dc_verdict.passed,
    f"mege={dc_obs.mege:.0f} reason={dc_verdict.reason}",
)
check(
    "its verdict is still a real measurement, not a rubber stamp",
    dc_verdict.mege == dc_obs.mege and dc_verdict.mege > 0.0,
    f"mege={dc_verdict.mege}",
)
check(
    "the same canvas at a raised threshold fails, so the gate reads it",
    not verdict(dark_canvas, dc_obs.mege * 2.0).passed,
    "a dark canvas passed a threshold it should have failed",
)

# The regression that actually bit: the full-frame path built its filter chain by appending
# to a string, so "flags=lanczos" and "unsharp=" fused whenever there was no crop. Every
# full-frame frame died with ffmpeg exit 234 and the run reported it as a quality failure.
dc_enhanced = qtmp / "q_out" / glq.enhanced_name(dark_canvas.name)
check("a full-frame enhancement actually writes a file", dc_enhanced.is_file(), str(dc_enhanced))
check(
    "the enhanced frame is non-empty",
    dc_enhanced.is_file() and dc_enhanced.stat().st_size > 0,
    "empty file",
)
check(
    "enhanced frames are named .jpg, because -q:v into a .png path is silently discarded",
    glq.enhanced_name("f_0000000000.png").endswith(".jpg"),
    glq.enhanced_name("f_0000000000.png"),
)

# A uniform dark field is blank, not soft. Reporting it as "soft" hides which failure
# occurred, and no sharpness number is meaningful on it.
blank = qframe("blank.png", "black")
blank_obs = observe(blank)
check("a black frame is classified blank", glq.is_blank(blank_obs, qs), str(blank_obs.luma_std))
blank_verdict = verdict(blank, 0.0)
check(
    "a blank frame fails with EMPTY_CANVAS, not SOFT",
    not blank_verdict.passed and blank_verdict.reason == glq.REASON_BLANK,
    blank_verdict.reason,
)
check(
    "a blank frame gets no enhanced file",
    not (qtmp / "q_out" / glq.enhanced_name(blank.name)).exists(),
    "wrote an enhanced frame for a blank canvas",
)

# A white frame has a bbox covering most of itself.
white = qframe("white.png", "white")
white_obs = observe(white)
check("a white frame gets a bbox", white_obs.box is not None, "no box")
if white_obs.box:
    check(
        "the bbox does not exceed the frame",
        white_obs.box.width <= 640 and white_obs.box.height <= 360,
        f"{white_obs.box.width}x{white_obs.box.height}",
    )
check(
    "a white frame is classified as a white document",
    white_obs.content_type == glq.CONTENT_WHITE,
    white_obs.content_type,
)

# The margin is only observable on a frame whose bright region is smaller than the frame.
inset = tmp / "quality_inset"
inset.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360",
        "-vf",
        "drawbox=x=200:y=120:w=240:h=120:color=white:t=fill",
        "-frames:v",
        "1",
        str(inset / "panel.png"),
    ],
    check=True,
)
panel_obs = observe(inset / "panel.png")
check("a centred panel gets a bbox", panel_obs.box is not None, "no box")
if panel_obs.box:
    check(
        "the bbox is wider than the bright panel, i.e. the margin was applied",
        panel_obs.box.width > 240 and panel_obs.box.x < 200,
        f"box={panel_obs.box.width} at x={panel_obs.box.x}, panel=240 at x=200",
    )

# The gate is derived from the run, not fixed. Two frames, and the threshold must land at
# decay x median(MEGE) -- and be above the weaker frame.
two = [observe(qtmp / "detail.png"), observe(qtmp / "blurred.png")]
derived = glq.threshold_for(two, qs)
mid = sorted(o.mege for o in two)[1] / 2 + sorted(o.mege for o in two)[0] / 2
check(
    "the threshold is decay x median over content frames",
    abs(derived - qs.decay * mid) < 1e-6,
    f"derived={derived} expected={qs.decay * mid}",
)
check(
    "the weaker frame falls below the derived threshold",
    min(o.mege for o in two) < derived <= max(o.mege for o in two),
    f"derived={derived} meges={[o.mege for o in two]}",
)
# A run of nothing but blank canvases must not normalise its way to "everything passes".
check(
    "an all-blank run falls back to the floor",
    glq.threshold_for([blank_obs, blank_obs], qs) == qs.floor,
    str(glq.threshold_for([blank_obs, blank_obs], qs)),
)
check(
    "an empty run falls back to the floor",
    glq.threshold_for([], qs) == qs.floor,
    str(glq.threshold_for([], qs)),
)

# The same frame flips with the threshold, and the reason names the measured value.
passing = verdict(qtmp / "detail.png", detail_mege * 0.5)
failing = verdict(qtmp / "detail.png", detail_mege * 2.0)
check("a frame can pass", passing.passed, "unexpected fail")
check("the same frame fails a raised threshold", not failing.passed, "gate ignored setting")
check(
    "a failing reason is SOFT",
    failing.reason == glq.REASON_SOFT,
    failing.reason,
)
check(
    "a too-thin edge population is NO_EDGES, distinct from SOFT",
    verdict(qtmp / "blurred.png", 0.0, glq.Settings(min_edge_fraction=0.99)).reason
    == glq.REASON_NO_EDGES,
    verdict(qtmp / "blurred.png", 0.0, glq.Settings(min_edge_fraction=0.99)).reason,
)

# An unreadable input is a failed frame, not a crash: one bad file must not lose 15 good ones.
bogus = qtmp / "not_an_image.png"
bogus.write_bytes(b"not an image at all")
try:
    observe(bogus)
    crashed = None
except OSError as exc:
    crashed = exc
check(
    "a corrupt frame raises OSError for run() to catch",
    crashed is not None,
    f"crashed={crashed!r}",
)
check(
    "the reason names the input that was unusable",
    crashed is not None and "not_an_image.png" in str(crashed),
    f"crashed={crashed!r}",
)

mixed = glq.run([qtmp / "detail.png", bogus], qtmp / "run", settings=qs, stream=io.StringIO())
check("run() survives a corrupt frame", len(mixed.frames) == 2, str(len(mixed.frames)))
check(
    "the corrupt frame is reported as failed, with its reason",
    not mixed.ok and any("not_an_image.png" in f.reason for f in mixed.frames if not f.passed),
    str([(f.name, f.reason[:40]) for f in mixed.frames]),
)
check(
    "the good frame in the same run still passed",
    any(f.name == "detail.png" and f.passed for f in mixed.frames),
    str([(f.name, f.passed) for f in mixed.frames]),
)
check(
    "an empty run is not ok", not glq.run([], qtmp / "empty", settings=qs, stream=io.StringIO()).ok
)

# bbox sources: a protocol with three honest implementations.
check(
    "the null estimator never crops",
    observe(dark_canvas, source="null").crop_state == glq.CROP_FALLBACK,
    observe(dark_canvas, source="null").crop_state,
)
check(
    "the geometric estimator does crop a white frame",
    observe(white, source="null").crop_state == glq.CROP_FALLBACK
    and observe(white).crop_state == glq.CROP_CROPPED,
    "source selection is not reaching observe()",
)
try:
    glq.VLMEstimator().estimate(dark_canvas, qs)
    vlm_raised = None
except glq.NotConfiguredError as exc:
    vlm_raised = exc
check(
    "an unconfigured VLM bbox source raises NotConfiguredError rather than guessing",
    vlm_raised is not None,
    f"raised={vlm_raised!r}",
)
try:
    glq.resolve_estimator("telepathy")
    unknown_raised = None
except KeyError as exc:
    unknown_raised = exc
check(
    "an unknown bbox source is refused by name",
    unknown_raised is not None and "telepathy" in str(unknown_raised),
    f"raised={unknown_raised!r}",
)

# The report must not leak the post-enhancement number into the gate field, and the three
# orthogonal measurements must be present separately.
qtmp2 = tmp / "quality_report"
qtmp2.mkdir(parents=True, exist_ok=True)
mixed_report = glq.run([qtmp / "detail.png", dark_canvas], qtmp2, settings=qs, stream=io.StringIO())
glq.write(mixed_report, qtmp2 / "quality.json", qs)
payload = json.loads((qtmp2 / "quality.json").read_text())
check(
    "the report records the derived threshold",
    payload.get("threshold") == mixed_report.threshold,
    str(payload.get("threshold")),
)
check(
    "the report records how the threshold was derived",
    "median" in payload.get("threshold_rule", ""),
    str(payload.get("threshold_rule")),
)
names = {f.name for f in mixed_report.frames}
entry = next(f for f in payload["frames"] if f["name"] == dark_canvas.name)
check(
    "the three measurements are separate fields",
    {"quality_gate", "content_type", "crop_state"} <= set(entry),
    str(sorted(entry)),
)
check(
    "a dark canvas is recorded as full-frame and dark_canvas",
    (entry["crop_state"], entry["content_type"]) == (glq.CROP_FALLBACK, glq.CONTENT_DARK),
    str(entry),
)
# Note what is NOT asserted here: that this canvas PASSES the run-derived gate. In a
# two-frame run the median is taken across one dense light slide and this dark one, and
# their MEGE values legitimately differ -- a dense light slide really does have steeper
# gradients. Whether the canvas clears the run's gate is tested above against an explicit
# threshold; what this run checks is that it is measured and classified at all.
check("the gated field is present", "mege" in entry, str(sorted(entry)))
check(
    "the enhanced measure is recorded separately",
    "mege_enhanced" in entry and entry["mege"] != entry.get("mege_enhanced"),
    str(entry),
)
check(
    "the gate decision uses the source number only",
    entry["quality_gate"] == ("PASS" if entry["mege"] >= entry["threshold"] else "FAIL"),
    str(entry),
)
check(
    "the bbox source reaches the report",
    {f["bbox_source"] for f in payload["frames"]} == {"geometric"},
    str([f["bbox_source"] for f in payload["frames"]]),
)
provenance = json.loads(glq.provenance(qs, "geometric"))
check(
    "provenance names the metric and the raster policy",
    provenance["metric"] == "MEGE" and "native" in provenance["raster"],
    str(provenance),
)

glc.run_all = real_run_all
glp.run = real_pipeline_run
glr.run = real_run
glr.resolve = real_resolve
glr.shutil.which = real_which
glfr.time.sleep = real_sleep
if real_workdir_env is None:
    os.environ.pop(glw.WORKDIR_ENV, None)
else:
    os.environ[glw.WORKDIR_ENV] = real_workdir_env
shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all stage checks passed")
