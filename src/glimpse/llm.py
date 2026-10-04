"""An OpenAI-compatible chat client, with nothing else in it.

No SDK. `urllib` and `json` cover the whole surface -- POST to `/chat/completions`, read
`choices[0].message.content` -- and an SDK would add a dependency whose failure modes nobody
here has measured, for one endpoint shape. It is also what makes the provenance chain in
ADR-0004 D4
possible: every call records the exact bytes sent and received, which is not something a
provider library hands you.

## The model id

A reply carries **two** ids and they are not the same question.

`Reply.model` is what was *requested* -- the id in `Config`, which is what a reader needs in
order to reproduce the call. `Reply.served_model` is what the endpoint *reported*, or `None`
when it reported nothing.

They can disagree, and on this host's gateway they always do: the gateway routes by provider
prefix and answers with the upstream's name instead, so `opencode-go/glm-5.3-flash` returns as
`glm-5.3-flash`. With `force-model-prefix: true` the reported id is not routable at all --
replaying it returns `400 unknown provider`. Writing that into the artefact would make the
provenance chain record a call that cannot be repeated, so the reported id is recorded beside
the requested one and the two are left to disagree in the open.

## What it does not do

- **Retries on 4xx.** A 400 means the request is wrong; repeating it identically just burns
  the lecture's time budget. 429 and 5xx are retried, because those are transient by
  definition.
- **Stream.** ADR-0001 D7 forbids it: stage 7's progress cannot be estimated from a token count, and
  a partial note is not a note.
- **Guess at a missing key.** The endpoint may be keyless (a local llama-swap, the tailnet
  gateway) or may require one. Unset means send no `Authorization` header, which both of
  those accept. Guessing a placeholder key would produce a 401 that looks like an auth
  problem when it is a config one.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ENDPOINT_ENV = "GLIMPSE_LLM_ENDPOINT"
MODEL_ENV = "GLIMPSE_LLM_MODEL"
KEY_ENV = "GLIMPSE_LLM_KEY"

#: Retries apply to 429 and 5xx only. A 4xx is a request the endpoint has already rejected on
#: its merits.
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RETRY_SLEEP = 2.0
RETRY_MAX = 3


class NotConfiguredError(RuntimeError):
    """No endpoint, or an endpoint with no model id. Raised before any network call."""


class EndpointError(RuntimeError):
    """The endpoint was reached and refused. Carries the status and the body it sent back."""


@dataclass(frozen=True)
class Config:
    endpoint: str
    model: str
    api_key: str | None = None
    timeout: float = 600.0
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> Config:
        endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
        model = os.environ.get(MODEL_ENV, "").strip()
        if not endpoint:
            raise NotConfiguredError(f"set {ENDPOINT_ENV} to enable synthesis, audit and repair")
        if not model:
            raise NotConfiguredError(f"{ENDPOINT_ENV} is set but {MODEL_ENV} is not")
        key = os.environ.get(KEY_ENV, "").strip() or None
        return cls(endpoint=endpoint.rstrip("/"), model=model, api_key=key)

    @property
    def url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    def redacted(self) -> dict:
        """Config safe to write into a bundle. The key never is."""
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key": bool(self.api_key),
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class Reply:
    text: str
    #: What was requested -- the id that can be replayed against this endpoint.
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    attempts: int
    #: What the endpoint said it served, or None when it said nothing. Recorded beside `model`
    #: rather than substituted for it; see the module docstring.
    served_model: str | None = None

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "served_model": self.served_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 3),
            "attempts": self.attempts,
        }


def chat(
    messages: list[dict],
    config: Config,
    *,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
) -> Reply:
    """One non-streaming completion. Raises `NotConfiguredError` or `EndpointError`."""
    payload: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if stop:
        payload["stop"] = stop

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    if config.api_key:
        request.add_header("Authorization", f"Bearer {config.api_key}")

    began = time.monotonic()
    last: EndpointError | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _to_reply(data, config.model, time.monotonic() - began, attempt)
        except urllib.error.HTTPError as exc:
            detail = _body(exc)
            last = EndpointError(f"HTTP {exc.code} from {config.url}: {detail}")
            if exc.code not in RETRY_STATUS:
                raise last from exc
        except urllib.error.URLError as exc:
            # Connection refused, DNS failure, timeout. All transient as far as this code can
            # tell, and all indistinguishable to it -- so they are retried and then reported.
            last = EndpointError(f"{config.url} unreachable: {exc.reason}")
        except json.JSONDecodeError as exc:
            # A body that is not JSON is a proxy or an error page, not a refusal. Retrying
            # cannot help and retrying a 500-shaped HTML page three times is worse.
            raise EndpointError(f"{config.url} returned non-JSON: {exc}") from exc
        if attempt < RETRY_MAX:
            time.sleep(RETRY_SLEEP * attempt)
    raise last if last else EndpointError("no attempt was made")


def _body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # noqa: BLE001 -- the body is the interesting part, not its failure
        return "<unreadable>"


def _to_reply(data: dict, model: str, seconds: float, attempts: int) -> Reply:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise EndpointError(
            f"response has no choices[0].message.content; got keys {sorted(data)}"
        ) from exc
    usage = data.get("usage") or {}
    echoed = data.get("model")
    return Reply(
        text=text.strip(),
        # The requested id, not the reported one. A gateway that routes by provider prefix
        # reports the *upstream's* name, which the endpoint itself cannot route: measured on
        # CLIProxyAPI, `opencode-go/glm-5.3-flash` comes back as `glm-5.3-flash`, and with
        # `force-model-prefix: true` replaying that id is a 400. Substituting it would put an
        # unreplayable id in the artefact that is supposed to record what was asked for, so
        # both are kept and the disagreement stays visible instead of being resolved silently.
        model=model,
        served_model=str(echoed) if echoed else None,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        seconds=seconds,
        attempts=attempts,
    )


@dataclass
class Transcript:
    """Every call made, for the provenance chain.

    Held in memory and written once. ADR-0004 D4 requires that a note can be traced to the exact
    model output it came from, and the cheapest way to guarantee that is to record it at the
    point of the call rather than reconstructing it later.
    """

    calls: list[dict] = field(default_factory=list)

    def record(self, stage: str, purpose: str, messages: list[dict], reply: Reply) -> None:
        self.calls.append(
            {
                "stage": stage,
                "purpose": purpose,
                "messages": messages,
                **reply.as_dict(),
            }
        )

    def as_dict(self) -> dict:
        return {"calls": self.calls}

    def write(self, target: Path) -> None:
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
