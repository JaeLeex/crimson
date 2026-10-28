"""The builders clip: two builders with the same model and brief, one under its lease and one not, side by side.

The enforced panel shows the live game throughout: a starting level until a proposal is accepted, then that quest.
Its refusals appear as a banner over the game, which keeps playing; the unenforced panel plays each proposal.

The first proposal is shown at length: in the two pressure runs (`crimson minds build --goal ...`, same seed) it is
the same proposal on both sides, so the left refuses it before it runs and the right plays it. The rest follow as a
ticker, one proposal per `step_s`: a refused proposal shows a card, a proposal that ran shows an excerpt of its
seed-1 playtest replay at game speed. The left stops at its budget cap; the right keeps building past its allowance.
The ticker is sped up (a proposal took 0.1 s of model time and a few seconds of playtests), and the clip says so.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import msgspec

from .build import Attempt
from .cards import _BOLD, _REGULAR, _font
from .compose import _AMBER, _BG, _GREEN, _GREY, _RED, _WHITE, _bar, _fade

_CARD = (22, 24, 30)
_DIM = (120, 125, 135)

# Plain names for the catalog in on-screen text.
PLAIN: dict[str, str] = {
    "nest.alien": "alien nests",
    "spider.boss": "spider bosses",
    "alien.boss": "alien bosses",
    "spider.plasma": "plasma spiders",
}


@dataclass
class Builds:
    """One build directory as the clip reads it."""

    path: Path
    attempts: list[Attempt]
    allowance: int
    # Excerpt videos of the playtests, by attempt index (rendered by the CLI).
    excerpts: dict[int, Path] = field(default_factory=dict)
    # When set, the panel shows the live game instead of each proposal: (from clip frame, video, from video frame),
    # starting on the baseline level and switching to each accepted quest. Refusals then appear as a banner over it.
    live: list[tuple[int, Path, int]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Builds:
        attempts = [msgspec.json.decode(line, type=Attempt) for line in (path / "attempts.jsonl").read_bytes().splitlines() if line]
        lease = json.loads((path / "lease-builder.json").read_text(encoding="utf-8"))
        return cls(path=path, attempts=attempts, allowance=int(lease["budget_credits"]))

    def creatures(self, attempt: Attempt) -> int:
        from ..quests.dsl import build_custom_quest

        if not attempt.recipe_file:
            return 0
        try:
            quest = build_custom_quest((self.path / attempt.recipe_file).read_text(encoding="utf-8"))
        except ValueError:
            return 0
        return sum(int(entry.count) for entry in quest.entries)


def violation_text(violation: str) -> str:
    """'nest.alien not allowed (x30)' -> '30 alien nests: not allowed'."""

    if " not allowed (x" in violation:
        name, count = violation.split(" not allowed (x")
        return f"{count.rstrip(')')} {PLAIN.get(name, name)}: not allowed"
    if " creatures > " in violation:
        count, cap = violation.split(" creatures > ")
        return f"{count} enemies: over the {cap} cap"
    return violation


@dataclass(frozen=True)
class Slot:
    """What one panel shows over a span of frames."""

    start: int
    frames: int
    attempt: Attempt | None
    # Frame offset into the excerpt video (the playtest replay rendered from tick 0).
    excerpt_from: int = 0


def schedule(builds: Builds, *, intro_frames: int, step_frames: int, total: int) -> list[Slot]:
    slots: list[Slot] = []
    frame = 0
    for index, attempt in enumerate(builds.attempts):
        length = intro_frames if index == 0 else step_frames
        if attempt.verdict == "refused_budget":
            slots.append(Slot(frame, total - frame, attempt))
            return slots
        if frame >= total:
            break
        # The first proposal is shown for the 8 s leading up to its playtest's end (by 21 s), where its waves are on
        # screen; the ticker shows each later one from 8 s in.
        start = 480
        if index == 0:
            end = int(attempt.playtests[0].seconds * 60) if attempt.playtests else length
            start = max(0, min(end, 1260) - length)
        slots.append(Slot(frame, min(length, total - frame), attempt, excerpt_from=start))
        frame += length
    return slots


def clip_frames(left: Builds, *, intro_frames: int = 480, step_frames: int = 75, hold_frames: int = 300) -> tuple[int, int]:
    """(total frames, the frame the left's budget cap shows): the ticker runs to the cap, then holds."""

    cap_index = next((i for i, a in enumerate(left.attempts) if a.verdict == "refused_budget"), len(left.attempts))
    cap_frame = intro_frames + (cap_index - 1) * step_frames
    return cap_frame + hold_frames, cap_frame


def compose_build_story(
    left: Builds,
    right: Builds,
    out: Path,
    *,
    ffmpeg: str,
    title: str,
    corner: str,
    names: tuple[str, str],
    intro_s: float = 8.0,
    step_s: float = 1.25,
    hold_s: float = 5.0,
    tile: int = 512,
) -> dict[str, object]:
    from PIL import Image, ImageDraw

    intro_frames = int(intro_s * 60)
    step_frames = int(step_s * 60)
    total, cap_frame = clip_frames(left, intro_frames=intro_frames, step_frames=step_frames, hold_frames=int(hold_s * 60))
    plans = [schedule(builds, intro_frames=intro_frames, step_frames=step_frames, total=total) for builds in (left, right)]

    title_h, meter_h, caption_h = 72, 248, 96
    width, height = 2 * tile, title_h + tile + meter_h + caption_h
    title_font, corner_font = _font(_BOLD, 34), _font(_REGULAR, 18)
    head, status_font, total_font = _font(_BOLD, 26), _font(_BOLD, 26), _font(_BOLD, 46)
    body, small, tag_font = _font(_REGULAR, 20), _font(_REGULAR, 17), _font(_BOLD, 18)
    card_big, card_body = _font(_BOLD, 30), _font(_REGULAR, 21)
    cue_font, caption_font = _font(_BOLD, 16), _font(_REGULAR, 24)

    def spent_by(builds: Builds, upto: int) -> int:
        return sum(a.cost for a in builds.attempts[: upto + 1])

    final_spend = [spent_by(b, max((s.attempt.attempt for s in p if s.attempt), default=0)) for b, p in zip((left, right), plans, strict=True)]
    scale = max(int(max(left.allowance, right.allowance) * 1.25), int(max(final_spend) * 1.1))
    meter_top = title_h + tile
    rows = {"name": meter_top + 8, "status": meter_top + 42, "total": meter_top + 74, "label": meter_top + 124,
            "bar": meter_top + 158, "cap": meter_top + 177, "props": meter_top + 200, "rules": meter_top + 223}
    bar_w = tile - 48
    caption_top = title_h + tile + meter_h

    decoders: dict[tuple[int, int], subprocess.Popen[bytes]] = {}
    last_frame: list[Image.Image | None] = [None, None]

    def excerpt_frame(side: int, builds: Builds, slot: Slot) -> Image.Image | None:
        assert slot.attempt is not None
        key = (side, slot.attempt.attempt)
        path = builds.excerpts.get(slot.attempt.attempt)
        if path is None:
            return None
        if key not in decoders:
            decoders[key] = subprocess.Popen(
                [ffmpeg, "-loglevel", "error", "-i", str(path), "-vf", f"select=gte(n\\,{slot.excerpt_from}),scale={tile}:{tile}",
                 "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE,
            )
        stream = decoders[key].stdout
        raw = stream.read(tile * tile * 3) if stream is not None else b""
        if len(raw) == tile * tile * 3:
            last_frame[side] = Image.frombytes("RGB", (tile, tile), raw)
        return last_frame[side]

    live_decoders: dict[int, subprocess.Popen[bytes]] = {}

    def live_frame(side: int, builds: Builds) -> Image.Image | None:
        index = max(i for i, (start, _, _) in enumerate(builds.live) if start <= frame)
        _, path, offset = builds.live[index]
        if index not in live_decoders:
            for old in live_decoders.values():
                old.kill()
            live_decoders.clear()
            live_decoders[index] = subprocess.Popen(
                [ffmpeg, "-loglevel", "error", "-i", str(path), "-vf", f"select=gte(n\\,{offset}),scale={tile}:{tile}",
                 "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE,
            )
        stream = live_decoders[index].stdout
        raw = stream.read(tile * tile * 3) if stream is not None else b""
        if len(raw) == tile * tile * 3:
            last_frame[side] = Image.frombytes("RGB", (tile, tile), raw)
        return last_frame[side]

    encoder = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "60", "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-an", str(out)],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    try:
        for frame in range(total):
            canvas = Image.new("RGB", (width, height), _BG)
            draw = ImageDraw.Draw(canvas)
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            glow = ImageDraw.Draw(overlay)
            draw.text((28, 18), title, fill=_WHITE, font=title_font)
            draw.text((width - 28 - draw.textlength(corner, font=corner_font), 30), corner, fill=_GREY, font=corner_font)
            for side, (builds, plan) in enumerate(zip((left, right), plans, strict=True)):
                x0 = side * tile
                slot = next((s for s in plan if s.start <= frame < s.start + s.frames), plan[-1])
                attempt = slot.attempt
                assert attempt is not None
                stopped = attempt.verdict == "refused_budget"
                shown = attempt.attempt - 1 if stopped else attempt.attempt
                spent = spent_by(builds, shown)
                done = builds.attempts[: shown + 1]
                accepted = sum(a.verdict == "accepted" for a in done)
                refused_rules = sum(a.verdict == "refused_policy" for a in done)
                ran_rules = sum(a.executed_violations for a in done)

                # The game area: a refused card, the budget-cap card, or the playtest excerpt.
                area = (x0, title_h, x0 + tile, title_h + tile)
                if builds.live:
                    image = live_frame(side, builds)
                    if image is not None:
                        canvas.paste(image, (x0, title_h))
                    if stopped:
                        lines = [("Budget cap reached: no more proposals", _AMBER, tag_font),
                                 ("The game continues on the last accepted level.", _WHITE, small)]
                    elif attempt.verdict == "refused_policy":
                        lines = [(f"Proposal #{attempt.attempt + 1} refused before it ran", _AMBER, tag_font)]
                        lines += [("· " + violation_text(v), _WHITE, small) for v in attempt.violations[:2]]
                    elif attempt.verdict == "accepted":
                        lines = [(f"Proposal #{attempt.attempt + 1} passed its playtest: now live", _GREEN, tag_font)]
                    elif attempt.verdict in ("rejected", "invalid"):
                        lines = [(f"Proposal #{attempt.attempt + 1} playtested: {attempt.detail.split(':')[0]}, not accepted", _GREY, tag_font)]
                    else:
                        lines = [(f"Proposal #{attempt.attempt + 1}: {attempt.detail}", _GREY, tag_font)]
                    level = "Live game: " + ("starting level" if not accepted else f"level from proposal #{max(a.attempt for a in done if a.verdict == 'accepted') + 1}")
                    height_tag = 34 + 24 * len(lines)
                    top = title_h + tile - height_tag
                    glow.rectangle((x0, top, x0 + tile, title_h + tile), fill=(10, 11, 14, 210))
                    glow.text((x0 + 16, top + 6), level, fill=(*_DIM, 255), font=small)
                    for row, (text, colour, font) in enumerate(lines):
                        glow.text((x0 + 16, top + 30 + 24 * row), text, fill=(*colour, 255), font=font)
                elif stopped:
                    draw.rectangle(area, fill=_CARD)
                    draw.text((x0 + 32, title_h + 170), "Budget cap reached", fill=_AMBER, font=card_big)
                    draw.text((x0 + 32, title_h + 214), "No more model calls.", fill=_WHITE, font=card_body)
                    draw.text((x0 + 32, title_h + 244), "The builder stopped at its allowance.", fill=_WHITE, font=card_body)
                elif attempt.verdict == "refused_policy":
                    draw.rectangle(area, fill=_CARD)
                    draw.text((x0 + 32, title_h + 120), f"Proposal #{attempt.attempt + 1}", fill=_DIM, font=card_body)
                    draw.text((x0 + 32, title_h + 152), "Refused before it ran", fill=_AMBER, font=card_big)
                    y = title_h + 204
                    for violation in attempt.violations[:3]:
                        draw.text((x0 + 32, y), "· " + violation_text(violation), fill=_WHITE, font=card_body)
                        y += 30
                    draw.text((x0 + 32, y + 16), f"The proposal was metered ({attempt.cost} credits).", fill=_DIM, font=card_body)
                    draw.text((x0 + 32, y + 46), "Nothing was played.", fill=_DIM, font=card_body)
                else:
                    image = excerpt_frame(side, builds, slot)
                    if image is not None:
                        canvas.paste(image, (x0, title_h))
                    # A tag along the bottom of the game: which proposal, and what became of it.
                    if attempt.executed_violations:
                        line, colour = "Ran anyway: " + ", ".join(violation_text(v).split(":")[0] for v in attempt.violations[:2]), _RED
                    elif attempt.verdict == "accepted":
                        line, colour = "Playtest passed: accepted", _GREEN
                    else:
                        line, colour = "Playtest: " + attempt.detail.split(":")[0], _GREY
                    top = title_h + tile - 56
                    glow.rectangle((x0, top, x0 + tile, title_h + tile), fill=(10, 11, 14, 200))
                    glow.text((x0 + 16, top + 6), f"Proposal #{attempt.attempt + 1} · {builds.creatures(attempt)} spawns · playtest excerpt", fill=(*_DIM, 255), font=small)
                    glow.text((x0 + 16, top + 29), line, fill=(*colour, 255), font=tag_font)

                # Status, the credits meter (fixed cap marker), and the rule counts.
                if stopped:
                    status, colour = "Budget cap · Builder stopped", _AMBER
                elif attempt.verdict == "refused_policy":
                    status, colour = "Refused before it ran", _AMBER
                elif attempt.executed_violations:
                    status, colour = "Ran despite the rules", _RED
                elif spent > builds.allowance:
                    status, colour = "Building · over allowance", _RED
                else:
                    status, colour = "Building", _GREEN
                if side == 0 and (accent := _fade(frame, cap_frame, 80)):
                    glow.rounded_rectangle((x0 + 12, rows["status"] - 3, x0 + tile - 12, rows["bar"] - 8), radius=8,
                                           fill=(*_AMBER, int(28 * accent)), outline=(*_AMBER, int(230 * accent)), width=3)
                x = x0 + 24
                draw.text((x, rows["name"]), names[side], fill=_WHITE, font=head)
                draw.text((x, rows["status"]), status, fill=colour, font=status_font)
                draw.text((x, rows["total"]), f"{spent} / {builds.allowance}", fill=_WHITE, font=total_font)
                draw.text((x, rows["label"]), "Credits used", fill=_GREY, font=body)
                _bar(draw, x, rows["bar"], bar_w, 14, spent, builds.allowance, scale)
                cap_text = f"{builds.allowance}-credit cap"
                cap_x = x + int(builds.allowance * bar_w / scale)
                draw.text((cap_x - draw.textlength(cap_text, font=body) / 2, rows["cap"]), cap_text, fill=_WHITE, font=body)
                draw.text((x, rows["props"]), f"Jev: {len(done)} proposals · {accepted} accepted", fill=_GREY, font=body)
                rules_colour = _RED if ran_rules else _GREY
                draw.text((x, rows["rules"]), f"Rule-breaking: {refused_rules} refused · {ran_rules} ran", fill=rules_colour, font=body)
            draw.line((tile, meter_top + 8, tile, meter_top + meter_h - 8), fill=(60, 64, 72), width=2)

            # Captions: the shared proposal, then the ticker, then the left stop.
            if frame >= cap_frame:
                draw.rectangle((24, caption_top + 10, 29, caption_top + 84), fill=_AMBER)
                draw.text((44, caption_top + 8), "LEFT · LEASE ENFORCED", fill=_AMBER, font=cue_font)
                draw.text((44, caption_top + 29), "Budget reached. The builder stops.", fill=_WHITE, font=caption_font)
                draw.text((44, caption_top + 57), "Right keeps building past its allowance.", fill=_WHITE, font=caption_font)
            elif frame >= intro_frames:
                draw.rectangle((24, caption_top + 16, 29, caption_top + 74), fill=_AMBER)
                draw.text((44, caption_top + 18), "Every proposal is metered and checked.", fill=_WHITE, font=caption_font)
                draw.text((44, caption_top + 46), "Left: refused levels never reach the game. Right: they run.", fill=_WHITE, font=caption_font)
            else:
                draw.rectangle((24, caption_top + 16, 29, caption_top + 74), fill=_AMBER)
                draw.text((44, caption_top + 18), "Same AI, same proposed level: nests and spider bosses.", fill=_WHITE, font=caption_font)
                draw.text((44, caption_top + 46), "The lease allows neither.", fill=_WHITE, font=caption_font)

            encoder.stdin.write(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB").tobytes())
    finally:
        encoder.stdin.close()
        encoder.wait()
        for decoder in [*decoders.values(), *live_decoders.values()]:
            decoder.kill()
    return {"frames": total, "cap_frame": cap_frame, "right_shown": max(s.attempt.attempt for s in plans[1] if s.attempt) + 1}
