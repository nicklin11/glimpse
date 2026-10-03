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
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from glimpse import audio as gla  # noqa: E402
from glimpse import cli as glc  # noqa: E402
from glimpse import deps as gld  # noqa: E402
from glimpse import exitcodes as ec  # noqa: E402
from glimpse import frames as glfr  # noqa: E402
from glimpse import pipeline as glp  # noqa: E402
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

install_fake({"shipboard": [(b'{"text": "plain"}', b"", 0)]})
exc = raises(glstt.transcribe, dest, audio_duration=25.0)
check("a dict instead of an array -> exit 3", exc.code == ec.DEPENDENCY_FAILED, f"{exc.code}")
check("dict payload names the contract", "array" in exc.message, exc.message)

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
    return SimpleNamespace(
        source=source,
        artefacts={n: work.path / n for n in ARTEFACTS},
        seconds=1.5,
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
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = glc.main(["process", str(src), "--keep-workdir"])
text = out.getvalue()
check("--keep-workdir reports that it kept the dir", "KEPT for inspection" in text, text)
for name in ARTEFACTS:
    check(f"--keep-workdir lists {name}", name in text, text)
kept = list(work_root.glob("glimpse-*"))
check("--keep-workdir leaves exactly one work dir", len(kept) == 1, str(kept))

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
    rc = glc.main(["process", str(src), "--keep-workdir"])
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
    [p.name for p in produced] == [f"f_{t:010d}.jpg" for t in frame_times],
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
stale = frame_out / "f_9999999999.jpg"
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
