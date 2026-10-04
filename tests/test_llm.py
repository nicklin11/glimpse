#!/usr/bin/env python3
"""A reply must be able to say which model it came from, and the bundle must not
contradict itself about it (#61).

Stage 7 writes two artefacts from one call: `synth.json` records
`config.model`, the id that was *requested*, and `synth-llm-transcript.json`
records `Reply.model`, which `_to_reply` fills from the response body's `model`
field -- the id the endpoint *reported*. When the two differ, the bundle holds
two answers to "which model wrote this note" and nothing marks which is which.

The gateway this repo is wired to does differ. Measured against
CLIProxyAPI at 100.64.0.2:8317, a request for `opencode-go/glm-5.3-flash` comes
back as `"model": "glm-5.3-flash"`. The prefix is stripped, and because that
gateway runs `force-model-prefix: true` the reported id is not routable -- a
reader who tries to replay it gets `400 unknown provider for model
glm-5.3-flash`. So the value written into the transcript cannot reproduce the
call it claims to record.

Which of the two ids wins is a separate decision (#61). What is not a decision
is that the bundle must be able to state the answer, and must not assert two
answers at once. That is what this script holds fixed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glimpse import llm  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


#: What was asked for. Prefixed, because this gateway only routes prefixed ids.
REQUESTED = "opencode-go/glm-5.3-flash"
#: What came back. The gateway strips the prefix.
ECHOED = "glm-5.3-flash"

BODY_ECHOED = {
    "choices": [{"message": {"content": "ok"}}],
    "model": ECHOED,
    "usage": {"prompt_tokens": 19, "completion_tokens": 3},
}
BODY_NO_MODEL = {
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 19, "completion_tokens": 3},
}

reply_echoed = llm._to_reply(BODY_ECHOED, REQUESTED, 0.1, 1)
reply_silent = llm._to_reply(BODY_NO_MODEL, REQUESTED, 0.1, 1)

# --- the requested id survives the round trip -------------------------------------

check(
    "a response with no model field falls back to what was requested",
    reply_silent.model == REQUESTED,
    f"{reply_silent.model}",
)

# --- the disagreement must be visible, not silent ---------------------------------
#
# `synth.json` gets `config.model` (synth.py:424 -> provenance, synth.py:457).
# The transcript gets `Reply.model` (llm.py:95, via Reply.as_dict). If the
# endpoint rewrote the id, those two artefacts currently disagree and neither
# says why.

config = llm.Config(endpoint="http://x/v1", model=REQUESTED)
provenance_model = config.model  # what synth.json will carry
transcript_model = reply_echoed.model  # what synth-llm-transcript.json will carry

check(
    "the reply states which model served it, not only which model was asked for",
    hasattr(reply_echoed, "served_model"),
    "Reply has no served_model field, so a rewritten id cannot be reported as one",
)

check(
    "the id written to the transcript is the one that was requested",
    transcript_model == REQUESTED,
    f"transcript={transcript_model} provenance={provenance_model}",
)

check(
    "synth.json and the transcript cannot name different models",
    provenance_model == transcript_model,
    f"synth.json={provenance_model} transcript={transcript_model}",
)

check(
    "a rewritten id is reported rather than absorbed",
    getattr(reply_echoed, "served_model", None) == ECHOED,
    "the endpoint's answer is discarded with nothing recording that it differed",
)

# --- the common case must not regress ---------------------------------------------
#
# Every gateway that echoes the id it was given is the boring case, and the fix
# must not turn it into a warning. Same id in, same id out, no noise.

BODY_MATCHES = {
    "choices": [{"message": {"content": "ok"}}],
    "model": REQUESTED,
}
reply_matching = llm._to_reply(BODY_MATCHES, REQUESTED, 0.1, 1)
check(
    "an echo that matches the request raises nothing and records one id",
    reply_matching.model == REQUESTED,
    f"{reply_matching.model}",
)

print()
if failures:
    print(f"FAIL  {len(failures)} model-id provenance check(s) failed:")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("all model-id provenance checks passed")