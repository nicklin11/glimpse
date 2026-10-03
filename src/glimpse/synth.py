"""Stage 7: turn an aligned lecture into a structured note.

The first stage that needs a model, and the only place the pipeline is allowed to be
creative. Everything before it measures; everything after it checks.

## Two implementations of one protocol

`Synthesizer` is a Protocol with two implementations, and the choice is recorded in
provenance either way:

- `LLMSynthesizer` calls the configured endpoint, one request per section.
- `TemplateSynthesizer` emits the same eight-section skeleton from the transcript alone,
  with no model at all.

`TemplateSynthesizer` exists so the pipeline is runnable and testable end to end without a
GPU, and so the failure mode of an unavailable endpoint is a *worse note* rather than no
note. D5 requires the note to survive a failed synthesis; the template is that guarantee
made concrete rather than aspirational. It is also the oracle: if the LLM output is worse
than a blank skeleton, the model is not earning its place in the pipeline.

## Why per-section, not one request

A 4 500-second lecture is 152 000 characters of ASR. That does not fit a context window with
room for instructions, and truncation from the wrong end loses the end of the lecture -- the
part a student reads the night before the exam. Sections are cut on frame boundaries, so
each request is bounded by something that means something visually rather than by a
character count that means nothing.

## The eight sections are not negotiable

D5 fixes them, and stage 8 lints against them. A section with no material in the transcript
is written as an explicit "not covered" line. An invented section is a lie about the
lecture; a missing one is a lie about the note's completeness. The second is recoverable,
the first is not.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import llm, stages

REPORT_NAME = "synth.json"
PROVENANCE_NAME = "synth-provenance.json"
NOTE_NAME = "note.md"

#: D5's structure. `(number, title, one line on what belongs here)`. The lint stage checks
#: the note against this list, so a section title that drifts here drifts in two places.
SECTIONS: tuple[tuple[int, str, str], ...] = (
    (1, "Паспорт лекции", "предмет, тема, лектор, executive summary"),
    (2, "Организационный блок", "требования, дедлайны, формат экзамена"),
    (3, "Карта источников", "упомянутые курсы, авторы, инструменты"),
    (4, "Понятийный аппарат и постановка задачи", "термины, формализация, ограничения"),
    (5, "Теоретический конспект и методы", "классификация, идея, границы, trade-off"),
    (6, "Математический фундамент", "формулы и их инженерный смысл"),
    (7, "Численная реализация и инструменты", "сравнение подходов, библиотеки, ловушки"),
    (8, "Вопросы для самопроверки", "вопросы на понимание, с критериями"),
)

NOT_COVERED = "в лекции не затрагивается"

SYSTEM = """\
Ты — конспектор инженерной лекции. Пиши конспект на русском, в разметке Markdown.

ЖЁСТКИЕ ПРАВИЛА:
1. Только то, что прозвучало в транскрипте. Ничего не добавляй от себя. Если нужно
   пояснить -- помечай [инженерная заметка].
2. Фрагмент, который не восстанавливается однозначно, помечай
   ⚠️ [неразборчиво: <буквально что услышали>]. Выдуманная формула хуже пропуска.
3. Вся математика в LaTeX: $...$ строчная, $$...$$ выключная. Векторы \\mathbf{x},
   матрицы \\mathbf{A}, скаляры без выделения.
4. Не выдумывай то, чего в лекции не было. Если раздел пуст -- напиши одну строку
   «в лекции не затрагивается».
5. Не пересказывай структуру этого документа. Не пиши воду.

ФОРМАТ: ответ — только тело раздела, без заголовка верхнего уровня и без обращений
к читателю. Разговорную речь («ну», «как бы», «вы поняли») вычисти."""


@dataclass(frozen=True)
class Section:
    """One slice of the lecture: the frames on screen while it was spoken."""

    number: int
    title: str
    brief: str
    start_ms: int
    end_ms: int
    text: str
    frames: tuple[str, ...] = ()
    word_count: int = 0

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "brief": self.brief,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "frames": list(self.frames),
            "word_count": self.word_count,
        }


@dataclass
class Note:
    markdown: str
    #: Per section: how it was produced and whether it looks complete. A section the model
    #: declined to fill is reported, not silently shipped.
    sections: list[dict] = field(default_factory=list)
    synthesizer: str = "template"
    model: str | None = None
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        return sum(1 for s in self.sections if s.get("present"))

    def as_dict(self) -> dict:
        return {
            "synthesizer": self.synthesizer,
            "model": self.model,
            "degraded": self.degraded,
            "sections": self.sections,
            "notes": self.notes,
            "bytes": len(self.markdown.encode("utf-8")),
        }


@runtime_checkable
class Synthesizer(Protocol):
    """What stage 7 needs from anything that can write prose."""

    name: str

    def section(self, section: Section, transcript: str) -> str:
        """The body of one of D5's eight sections. No top-level heading."""


# --- section cutting ---------------------------------------------------------------


def build_sections(alignments: list[dict], limit: int = 12) -> list[Section]:
    """Cut the lecture into bounded slices on frame boundaries.

    Frames, not character counts: a section that ends mid-sentence because it hit a token
    limit is a section nobody can read. Slicing on frame boundaries means each chunk is a
    stretch of lecture during which one thing was on screen.

    Empty frames are collapsed into the preceding slice rather than becoming their own
    section, so a gap in frame detection does not become a heading with no content.
    """
    usable = [a for a in alignments if a.get("caption_status") != "GATED_OUT"]
    if not usable:
        return []
    per = max(1, (len(usable) + limit - 1) // limit)

    out: list[Section] = []
    cursor = 0
    number = 1
    while cursor < len(usable):
        chunk = usable[cursor : cursor + per]
        text = " ".join((c.get("text") or "").strip() for c in chunk).strip()
        frames = tuple(c["frame"] for c in chunk)
        out.append(
            Section(
                number=number,
                title=f"Фрагмент {number}",
                brief=f"кадры {chunk[0]['frame']}–{chunk[-1]['frame']}",
                start_ms=chunk[0].get("on_screen_ms", [0, 0])[0],
                end_ms=chunk[-1].get("on_screen_ms", [0, 0])[-1],
                text=text,
                frames=frames,
                word_count=sum((c.get("word_count") or 0) for c in chunk),
            )
        )
        number += 1
        cursor += per
    return out


# --- template synthesizer -----------------------------------------------------------


class TemplateSynthesizer:
    """The eight-section skeleton, filled from the transcript. No model.

    Deterministic, and therefore the only synthesizer whose output can be asserted in a test.
    """

    name = "template"

    def __init__(self, source: str, transcript: str, sections: list[Section]) -> None:
        self.source = source
        self.transcript = transcript
        self.sections = sections

    def section(self, section: Section, _transcript: str) -> str:
        # The template does not invent content. It reports what it has, and says plainly
        # that the prose is missing. A reader can tell this is not a finished note.
        body = (
            f"**Источник:** `{Path(self.source).name}`, {len(self.transcript)} символов "
            f"транскрипта.\n\n"
            f"**Окно:** {section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с, "
            f"{len(section.frames)} кадров, {section.word_count} слов.\n\n"
            f"**Предобработка не выполнена.** Этот конспект собран без модели "
            f"синтеза: доступный текст стенограммы сохранён в `transcript.txt`, "
            f"структура разделов и тайминги кадров — в `captions.json`.\n\n"
            "> Синтез без модели не восстанавливает термины, не оформляет математику и "
            "не отделяет учебный материал от организационного. Это каркас, а не конспект."
        )
        return body


# --- llm synthesizer ----------------------------------------------------------------


class LLMSynthesizer:
    """One request per section, against the configured OpenAI-compatible endpoint."""

    name = "llm"

    def __init__(self, config: llm.Config, transcript: llm.Transcript) -> None:
        self.config = config
        self.transcript = transcript

    def section(self, section: Section, transcript: str) -> str:
        # Truncated from the front of this section's text, never the transcript: the tail
        # of a slide is what a student needs, and a token limit that eats the end is a
        # silent content loss.
        budget = 12_000
        body = transcript if len(transcript) <= budget else transcript[:budget] + "\n[…]\n"
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Раздел {section.number} из 8: «{section.title}».\n"
                    f"Что в нём должно быть: {section.brief}\n\n"
                    f"Тайминг: {section.start_ms / 1000:.0f}–{section.end_ms / 1000:.0f} с. "
                    f"Кадры на экране: {', '.join(section.frames) or 'нет'}.\n\n"
                    f"Стенограмма этого фрагмента:\n\n{body}\n"
                ),
            },
        ]
        reply = llm.chat(messages, self.config)
        self.transcript.record(7, f"section {section.number}", messages, reply)
        return reply.text


# --- note assembly ------------------------------------------------------------------


def assemble(note_title: str, bodies: dict[int, str], sections: list[Section]) -> str:
    """Interleave the eight required sections with whatever the synthesizer produced.

    The headings come from `SECTIONS`, never from the model. A model that emits its own
    heading would renumber the note, and stage 8's structural lint would then be checking a
    document that changed shape to satisfy it.
    """
    # The provenance footer goes directly under the title, not at the end. Appended last it
    # would land inside the final section's body -- and stage 8's
    # `claims-empty-but-is-not` rule then reads the footer as that section's content, which
    # is a false positive caused by where the text sits, not by what it says.
    parts = [f"# {note_title}", ""]
    if sections:
        parts.append(
            "> Собрано из выравненных кадров и стенограммы. Тайминги — `captions.json`, "
            "решения качества — `quality.json`, структура синтеза — `synth.json`."
        )
        parts.append("")
    for number, title, brief in SECTIONS:
        body = bodies.get(number, "").strip()
        parts.append(f"## {number}. {title}")
        parts.append("")
        if not body:
            parts.append(NOT_COVERED)
        else:
            parts.append(body)
        parts.append("")
    return "\n".join(parts)


#: A heading that only exists in the document's own table of contents.
_TOC = re.compile(r"^\s{0,3}#{1,6}\s*(содержание|оглавление)\b", re.IGNORECASE)

#: `#` at any level, which D5 does not permit inside a section. The eight section headings
#: are emitted by `assemble`; a model adding its own top level reshapes the document so the
#: structure lint then checks a different thing than it was written to check.
_H1 = re.compile(r"^(\s{0,3})#(?!#)\s+(.*)$")


def normalise(body: str) -> str:
    """Demote a model's top-level headings to third level. `##` is left alone.

    Demoting rather than deleting: the heading usually carries real information -- which
    slide the fragment was -- and the failure to preserve it is worse than the extra level.
    Stripping it would leave a note with unexplained structural breaks in it.
    """
    out = []
    for line in body.splitlines():
        out.append(_H1.sub(r"\1### \2", line))
    return "\n".join(out)


def synthesise(
    alignments: list[dict],
    transcript_text: str,
    source: Path,
    synthesizer: Synthesizer,
    *,
    title: str | None = None,
    model: str | None = None,
) -> Note:
    sections = build_sections(alignments)
    note_title = title or f"Конспект — {Path(source).name}"
    bodies: dict[int, str] = {}
    reported: list[dict] = []
    warnings: list[str] = []

    # The eight content sections map onto the numbered slices in order. A lecture with two
    # frames cannot support eight sections and pretending otherwise would be the exact
    # invention D5 forbids.
    for index, (number, section_title, brief) in enumerate(SECTIONS):
        payload = sections[index].text if index < len(sections) else ""
        if not payload.strip():
            reported.append(
                {
                    "number": number,
                    "title": section_title,
                    "present": False,
                    "reason": "no source material for this section",
                }
            )
            continue
        try:
            body = synthesizer.section(sections[index], payload)
        except llm.NotConfiguredError as exc:
            warnings.append(f"section {number}: {exc}")
            body = ""
        except llm.EndpointError as exc:
            # One failed section must not lose the seven that succeeded. D5: the note
            # survives a failed synthesis.
            warnings.append(f"section {number}: {exc}")
            body = ""
        first = body.splitlines()[0] if body.splitlines() else ""
        if first and _TOC.match(first):
            # A model told "section 5 of 8" will sometimes answer with the contents of the
            # whole document. Shipping that as section 5 duplicates eight headings and puts
            # a table of contents where the mathematics belongs.
            body = ""
            warnings.append(f"section {number}: model emitted a table of contents, discarded")
        else:
            body = normalise(body)
        bodies[number] = body
        reported.append(
            {
                "number": number,
                "title": section_title,
                "present": bool(body.strip()),
                "bytes": len(body.encode("utf-8")),
                "source_section": sections[index].title,
            }
        )

    markdown = assemble(note_title, bodies, sections)
    missing = [n for n, _, _ in SECTIONS if not bodies.get(n, "").strip()]
    return Note(
        markdown=markdown,
        sections=reported,
        synthesizer=getattr(synthesizer, "name", "unknown"),
        model=model,
        degraded=bool(warnings or missing),
        notes=warnings,
    )


def choose(env_config: llm.Config | None, fallback: Synthesizer) -> Synthesizer:
    """The configured synthesizer, or the template. Records which, without hiding it."""
    return LLMSynthesizer(env_config, llm.Transcript()) if env_config else fallback


def run(
    captions: Path,
    transcript_txt: Path,
    source: Path,
    destination: Path,
    *,
    config: llm.Config | None = None,
    title: str | None = None,
    stream=None,
) -> Note:
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    alignments = json.loads(captions.read_text(encoding="utf-8")).get("alignments", [])
    transcript = transcript_txt.read_text(encoding="utf-8") if transcript_txt.is_file() else ""

    try:
        config = config if config is not None else llm.Config.from_env()
    except llm.NotConfiguredError:
        config = None

    synthesizer = choose(config, TemplateSynthesizer(source, transcript, []))
    note = synthesise(
        alignments,
        transcript,
        source,
        synthesizer,
        title=title,
        model=config.model if config else None,
    )

    (destination / NOTE_NAME).write_text(note.markdown, encoding="utf-8")
    write(note, destination / REPORT_NAME)
    (destination / PROVENANCE_NAME).write_text(provenance(note, config) + "\n", encoding="utf-8")
    if isinstance(synthesizer, LLMSynthesizer):
        synthesizer.transcript.write(destination / "synth-llm-transcript.json")

    out.write(
        f"  [7/{stages.IMPLEMENTED}] synth    {note.synthesizer}, "
        f"{note.filled}/{len(SECTIONS)} sections filled"
        + ("  DEGRADED" if note.degraded else "")
        + "\n"
    )
    for warning in note.notes[:5]:
        out.write(f"           note: {warning}\n")
    return note


def write(note: Note, target: Path) -> None:
    target.write_text(
        json.dumps(note.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def provenance(note: Note, config: llm.Config | None) -> str:
    return json.dumps(
        {
            "stage": 7,
            "tool": "glimpse-synth-v1",
            "requires_model": False,
            "synthesizer": note.synthesizer,
            "model": note.model,
            "endpoint": config.redacted() if config else None,
            "degraded": note.degraded,
            "sections_required": [n for n, _, _ in SECTIONS],
            "sections_filled": [s["number"] for s in note.sections if s.get("present")],
            "why_two_implementations": (
                "TemplateSynthesizer runs with no model so the pipeline is end-to-end "
                "runnable and testable; it is also the oracle an LLM output must beat. "
                "D5 requires the note to survive a failed synthesis."
            ),
        },
        indent=2,
        ensure_ascii=False,
    )
