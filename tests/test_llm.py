#!/usr/bin/env python3
"""A reply must be able to say which model it came from, and the bundle must not
contradict itself about it (#61).

Stage 7 writes two artefacts from one call: `synth.json` records `config.model`
and `synth-llm-transcript.json` records `Reply.model`. Both are the id that was
*requested*. What the endpoint *reported* is a third value, `Reply.served_model`,
recorded beside them rather than allowed to overwrite them.

The gateway this repo is wired to does rewrite the id. Measured against
CLIProxyAPI at 100.64.0.2:8317, a request for `opencode-go/glm-5.3-flash` comes
back as `"model": "glm-5.3-flash"`. The prefix is stripped, and because that
gateway runs `force-model-prefix: true` the reported id is not routable -- a
reader who tries to replay it gets `400 unknown provider for model
glm-5.3-flash`. So the value written into the transcript cannot reproduce the
call it claims to record.

Which of the two ids wins was a separate decision (#61), now resolved: the requested
id is canonical, because it is the only one of the two that can be replayed against this
endpoint. What is not optional is that the bundle can state the answer, and never asserts
two answers to one question without marking which is which. That is what this script holds
fixed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

SAVED_ENV = _env.isolate()


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
# The transcript gets `Reply.model` (via Reply.as_dict). Both are the requested id, so
# they agree by construction; `served_model` is what carries the endpoint's answer, and a
# bundle that omitted it would have no way to say the two ever differed.

config = llm.Config(endpoint="http://x/v1", model=REQUESTED)
provenance_model = config.model  # what synth.json will carry
transcript_model = reply_echoed.model  # what synth-llm-transcript.json will carry

check(
    "the reply states which model served it, not only which model was asked for",
    hasattr(reply_echoed, "served_model"),
    f"served_model={getattr(reply_echoed, 'served_model', '<absent>')}",
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
    f"requested={REQUESTED} served={getattr(reply_echoed, 'served_model', '<absent>')}",
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

_env.restore(SAVED_ENV)

print()
if failures:
    print(f"FAIL  {len(failures)} model-id provenance check(s) failed:")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("all model-id provenance checks passed")
