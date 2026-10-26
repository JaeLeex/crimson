"""Compose minds videos at full resolution: game footage on top, large readable meters under it (Pillow + ffmpeg).

The in-game strip (`video.make_callbacks(strip=True)`) is drawn in the game's 16 px bitmap font inside a 512 px
frame, which is too small once two or four games share a screen. Here each game is rendered with only the enemy
rings and tints, then every output frame is laid out with Pillow: a title band, the games, and under each game a
meter block (status, a usage bar against the allowance, and the AI cost so far). One rendered frame is one game
tick (60 per second), so the meters are read off the run's receipts at that tick.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .cards import _BOLD, _REGULAR, _font
from .video import MIND_BYTES, MeterTimeline, MindTrack

_BG = (14, 16, 20)
_WHITE = (255, 255, 255)
_GREY = (165, 170, 180)
_GREEN = (110, 225, 120)
_AMBER = (255, 185, 50)
_RED = (255, 80, 70)


@dataclass(frozen=True)
class Panel:
    video: Path
    timeline: MeterTimeline
    name: str
    enforced: bool
    # The recording's length in ticks; after it ends the tile holds its last frame and the meters stop.
    ticks: int
    # Per rendered frame, the (slot, x, y, radius) of each enemy ring drawn on it (see `video.make_callbacks`).
    rings: list[list[tuple[int, int, int, int]]] = field(default_factory=list)


def _status(track: MindTrack, tick: int, enforced: bool) -> tuple[str, tuple[int, int, int]]:
    if track.revoke_tick is not None and tick >= track.revoke_tick:
        return "Revoked · Game continues", _RED
    if track.cap_tick is not None and tick >= track.cap_tick:
        return "Budget cap · Game continues", _AMBER
    if not enforced and track.spent(tick) > track.budget:
        return "AI active · over allowance", _RED
    return "AI active", _GREEN


def _bar(draw, x: int, y: int, w: int, h: int, used: int, allowance: int, scale_max: int) -> None:
    """Usage consumed, against an allowance line; past the allowance the bar continues in red."""

    draw.rectangle((x, y, x + w, y + h), fill=(48, 52, 60))
    unit = w / max(1, scale_max)
    within = min(used, allowance)
    draw.rectangle((x, y, x + int(within * unit), y + h), fill=(90, 200, 110) if used < allowance * 0.85 else _AMBER)
    if used > allowance:
        draw.rectangle((x + int(allowance * unit), y, x + int(min(used, scale_max) * unit), y + h), fill=_RED)
    line_x = x + int(allowance * unit)
    draw.rectangle((line_x - 1, y - 6, line_x + 1, y + h + 6), fill=_WHITE)


def _money(usd: float) -> str:
    return f"${usd:.4f}" if usd else "$0"


def _decode(ffmpeg: str, path: Path, size: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [ffmpeg, "-loglevel", "error", "-i", str(path), "-vf", f"scale={size}:{size}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
    )


def compose(
    panels: list[Panel],
    out: Path,
    *,
    ffmpeg: str,
    title: str,
    corner: str,
    legend: str,
    frames: int,
    tile: int = 512,
    scale_max: int | None = None,
) -> None:
    """Write `frames` frames (60 per second) of `panels` (2 side by side, or 4 in a 2x2 grid) to `out`."""

    from PIL import Image, ImageDraw

    columns = 2
    rows = 1 if len(panels) == 2 else 2
    title_h = 96
    meter_h = 280 if rows == 1 else 330
    legend_h = 40
    width = columns * tile
    height = title_h + rows * (tile + meter_h) + legend_h
    height += height % 2
    big = _font(_BOLD, 40)
    head = _font(_BOLD, 32 if rows == 1 else 24)
    status_font = _font(_BOLD, 32 if rows == 1 else 20)
    body = _font(_REGULAR, 24 if rows == 1 else 17)
    small = _font(_REGULAR, 18)
    def panel_scale(group: list[Panel]) -> int:
        tallest = max(max(track.spent(10**9) for track in panel.timeline.tracks) for panel in group)
        allowance = max(max(track.budget for track in panel.timeline.tracks) for panel in group)
        return scale_max or max(int(allowance * 1.25), int(tallest * 1.08))

    # Two panels are a comparison, so they share one scale; four are separate stories, so each reads on its own.
    scales = [panel_scale(panels)] * len(panels) if len(panels) == 2 else [panel_scale([panel]) for panel in panels]

    decoders = [_decode(ffmpeg, panel.video, tile) for panel in panels]
    frame_bytes = tile * tile * 3
    last: list[Image.Image | None] = [None] * len(panels)
    encoder = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "60", "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", str(out)],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    try:
        for frame in range(frames):
            tick = frame
            canvas = Image.new("RGB", (width, height), _BG)
            draw = ImageDraw.Draw(canvas)
            draw.text((28, 24), title, fill=_WHITE, font=big)
            draw.text((width - 28 - draw.textlength(corner, font=small), 36), corner, fill=_GREY, font=small)
            for index, panel in enumerate(panels):
                decoder = decoders[index]
                assert decoder.stdout is not None
                raw = decoder.stdout.read(frame_bytes)
                if len(raw) == frame_bytes:
                    last[index] = Image.frombytes("RGB", (tile, tile), raw)
                column, row = index % columns, index // columns
                x0 = column * tile
                y0 = title_h + row * (tile + meter_h)
                image = last[index]
                if image is not None:
                    canvas.paste(image, (x0, y0))
                # Game time stops advancing once a recording ends (the tile holds its last frame).
                game_tick = min(tick, max(0, panel.ticks - 1))
                y = y0 + tile + 16
                draw.text((x0 + 24, y), panel.name, fill=_WHITE, font=head)
                y += int(head.size * 1.45)
                for track in panel.timeline.tracks:
                    status, colour = _status(track, game_tick, panel.enforced)
                    if len(panel.timeline.tracks) > 1:
                        swatch = MIND_BYTES[track.mind_id % len(MIND_BYTES)]
                        draw.rectangle((x0 + 24, y + 6, x0 + 24 + 14, y + 20), fill=swatch)
                        draw.text((x0 + 46, y), f"controller {track.mind_id}: {status}", fill=colour, font=status_font)
                    else:
                        draw.text((x0 + 24, y), status, fill=colour, font=status_font)
                    y += int(status_font.size * 1.4)
                    used = track.spent(game_tick)
                    _bar(draw, x0 + 24, y, tile - 48, 16 if rows == 1 else 12, used, track.budget, scales[index])
                    y += (16 if rows == 1 else 12) + 10
                    draw.text((x0 + 24, y), f"Usage {used:,} / {track.budget:,} demo credits allowed", fill=_WHITE, font=body)
                    y += int(body.size * 1.35)
                    calls = sum(1 for call in track.calls if call.tick <= game_tick)
                    usd = track.usd(game_tick)
                    draw.text((x0 + 24, y), f"{track.model}: {calls} calls · {track.tokens(game_tick):,} tokens", fill=_GREY, font=body)
                    y += int(body.size * 1.3)
                    cost = f"Est. model cost {_money(usd)}" if track.usd_per_mtok else "Self-hosted model, no API charge"
                    draw.text((x0 + 24, y), cost, fill=_GREY, font=body)
                    y += int(body.size * 1.6)
                if index % columns == 0:
                    draw.line((tile, y0 + tile + 8, tile, y0 + tile + meter_h - 8), fill=(60, 64, 72), width=2)
            draw.text((28, height - legend_h + 8), legend, fill=_GREY, font=small)
            encoder.stdin.write(canvas.tobytes())
    finally:
        encoder.stdin.close()
        encoder.wait()
        for decoder in decoders:
            decoder.kill()



_CALLOUT_FRAMES = 90  # follow one ringed enemy for 1.5 s
_ACCENT_FRAMES = 80  # each amber accent lasts about 1.3 s
_FADE = 8


def _fade(frame: int, start: int | None, length: int) -> float:
    """0..1 opacity of an accent shown for `length` frames from `start`, with short fades at both ends."""

    if start is None or not start <= frame < start + length:
        return 0.0
    return min(1.0, (frame - start + 1) / _FADE, (start + length - frame) / _FADE)


def _ring_frames(panel: Panel, frames: int) -> list[list[tuple[int, int, int, int]]]:
    return [panel.rings[frame] if frame < len(panel.rings) else [] for frame in range(frames)]


def _pick_callout(rings: list[list[tuple[int, int, int, int]]], first: int, tile: int) -> tuple[int, int] | None:
    """The first (frame, slot) from which one ringed enemy stays in open ground for the callout's span.

    Open ground keeps the callout off the game HUD (the top bar and the timer/XP stack at the top left) and the edges.
    """

    def clear(x: int, y: int, slack: int = 0) -> bool:
        in_frame = 60 - slack <= x <= tile - 60 + slack and 90 - slack <= y <= tile - 110 + slack
        return in_frame and not (x < 170 + slack and y < 290 + slack)

    for start in range(first + 30, min(len(rings) - _CALLOUT_FRAMES, first + 360)):
        for slot, x, y, _ in rings[start]:
            if not clear(x, y):
                continue
            span = rings[start:start + _CALLOUT_FRAMES]
            if all(any(entry[0] == slot and clear(entry[1], entry[2], 30) for entry in entries) for entries in span):
                return start, slot
    return None


def compose_story(
    panels: list[Panel],
    out: Path,
    *,
    ffmpeg: str,
    title: str,
    corner: str,
    frames: int,
    tile: int = 512,
) -> dict[str, object]:
    """The two-panel application clip: budget enforced (left) against budget control disabled (right).

    Under each game: name, status, the live usage total as the primary number, a usage bar on one fixed shared scale with
    the allowance marker and its label pinned in place, then the model, calls, tokens and estimated cost. Along the bottom
    a timed caption: first what the orange rings mean (with a short callout following one real ringed enemy), then, from
    the frame the left panel's last ring disappears, that the left run reached its budget and the game continues on its
    built-in AI. Short amber accents mark the left status change and then the right run's overrun, one at a time.
    """

    from PIL import Image, ImageDraw

    assert len(panels) == 2
    title_h, meter_h, caption_h = 72, 248, 96
    width = 2 * tile
    height = title_h + tile + meter_h + caption_h
    height += height % 2
    title_font = _font(_BOLD, 36)
    corner_font = _font(_REGULAR, 18)
    head = _font(_BOLD, 26)
    status_font = _font(_BOLD, 26)
    total_font = _font(_BOLD, 46)
    label = _font(_REGULAR, 20)
    body = _font(_REGULAR, 20)
    cue_font = _font(_BOLD, 16)
    caption_font = _font(_REGULAR, 24)
    caption_bold = _font(_BOLD, 24)

    tallest = max(max(track.spent(10**9) for track in panel.timeline.tracks) for panel in panels)
    allowance = max(max(track.budget for track in panel.timeline.tracks) for panel in panels)
    scale = max(int(allowance * 1.25), int(tallest * 1.08))

    rings = [_ring_frames(panel, frames) for panel in panels]
    left = panels[0].timeline.tracks[0]
    first_ring = next((frame for frame in range(frames) if rings[0][frame]), 0)
    cap = left.cap_tick
    # The handoff caption starts when the left panel's rings are gone for good, not at the cap itself: the last
    # steering order still holds for a moment after the cap.
    last_left_ring = max((frame for frame in range(frames) if rings[0][frame]), default=-1)
    handoff = max(last_left_ring + 1, cap) if cap is not None else None
    callout = _pick_callout(rings[0], first_ring, tile)
    left_accent = cap
    # One focus at a time: the right accent follows the left one once the handoff caption is up.
    right_accent = max(handoff, cap) + _ACCENT_FRAMES + 6 if handoff is not None and cap is not None else None

    # Fixed geometry, identical in both panels for the whole clip.
    meter_top = title_h + tile
    rows = {
        "name": meter_top + 8, "status": meter_top + 42, "total": meter_top + 74, "label": meter_top + 124,
        "bar": meter_top + 158, "cap": meter_top + 177, "model": meter_top + 200, "cost": meter_top + 223,
    }
    bar_h = 14
    bar_w = tile - 48
    cap_offset = int(allowance * bar_w / scale)
    caption_top = title_h + tile + meter_h

    decoders = [_decode(ffmpeg, panel.video, tile) for panel in panels]
    frame_bytes = tile * tile * 3
    last: list[Image.Image | None] = [None, None]
    encoder = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "60", "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-an", str(out)],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    try:
        for frame in range(frames):
            canvas = Image.new("RGB", (width, height), _BG)
            draw = ImageDraw.Draw(canvas)
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            glow = ImageDraw.Draw(overlay)
            draw.text((28, 18), title, fill=_WHITE, font=title_font)
            draw.text((width - 28 - draw.textlength(corner, font=corner_font), 30), corner, fill=_GREY, font=corner_font)
            for index, panel in enumerate(panels):
                decoder = decoders[index]
                assert decoder.stdout is not None
                raw = decoder.stdout.read(frame_bytes)
                if len(raw) == frame_bytes:
                    last[index] = Image.frombytes("RGB", (tile, tile), raw)
                x0 = index * tile
                image = last[index]
                if image is not None:
                    canvas.paste(image, (x0, title_h))
                track = panel.timeline.tracks[0]
                game_tick = min(frame, max(0, panel.ticks - 1))
                status, colour = _status(track, game_tick, panel.enforced)
                used = track.spent(game_tick)
                calls = sum(1 for call in track.calls if call.tick <= game_tick)

                accent = _fade(frame, left_accent if index == 0 else right_accent, _ACCENT_FRAMES)
                if accent:
                    box = (x0 + 12, rows["status"] - 3, x0 + tile - 12, rows["bar"] - 8)
                    glow.rounded_rectangle(box, radius=8, fill=(*_AMBER, int(28 * accent)), outline=(*_AMBER, int(230 * accent)), width=3)

                x = x0 + 24
                draw.text((x, rows["name"]), panel.name, fill=_WHITE, font=head)
                draw.text((x, rows["status"]), status, fill=colour, font=status_font)
                draw.text((x, rows["total"]), f"{used:,} / {track.budget:,}", fill=_WHITE, font=total_font)
                draw.text((x, rows["label"]), "Credits used", fill=_GREY, font=label)
                _bar(draw, x, rows["bar"], bar_w, bar_h, used, track.budget, scale)
                cap_x = x + cap_offset
                cap_text = f"{track.budget:,}-credit cap"
                draw.text((cap_x - draw.textlength(cap_text, font=body) / 2, rows["cap"]), cap_text, fill=_WHITE, font=body)
                draw.text((x, rows["model"]), f"{track.model}: {calls} calls · {track.tokens(game_tick):,} tokens", fill=_GREY, font=body)
                cost = f"Est. model cost {_money(track.usd(game_tick))}" if track.usd_per_mtok else "Self-hosted model, no API charge"
                draw.text((x, rows["cost"]), cost, fill=_GREY, font=body)
            draw.line((tile, meter_top + 8, tile, meter_top + meter_h - 8), fill=(60, 64, 72), width=2)

            # The opening callout follows one real ringed enemy on the left for a moment.
            strength = _fade(frame, callout[0] if callout else None, _CALLOUT_FRAMES)
            if callout and strength:
                spot = next((entry for entry in rings[0][frame] if entry[0] == callout[1]), None)
                if spot is not None:
                    _, rx, ry, radius = spot
                    text = "AI model control"
                    tw = draw.textlength(text, font=cue_font)
                    below = ry + radius + 70 < tile - 10
                    bx = min(max(12, rx - tw / 2 - 10), tile - tw - 32)
                    by = ry + radius + 34 if below else ry - radius - 64
                    alpha = int(255 * strength)
                    edge_y = ry + radius + 2 if below else ry - radius - 2
                    glow.line((rx, title_h + edge_y, bx + tw / 2 + 10, title_h + (by if below else by + 28)), fill=(*_AMBER, alpha), width=3)
                    glow.rounded_rectangle((bx, title_h + by, bx + tw + 20, title_h + by + 28), radius=5, fill=(14, 16, 20, int(220 * strength)), outline=(*_AMBER, alpha), width=2)
                    glow.text((bx + 10, title_h + by + 5), text, fill=(*_AMBER, alpha), font=cue_font)

            # Bottom caption, under the left panel: what the rings mean, then the left run's handoff.
            if handoff is not None and frame >= handoff:
                draw.rectangle((24, caption_top + 10, 29, caption_top + 84), fill=_AMBER)
                draw.text((44, caption_top + 8), "LEFT · BUDGET ENFORCED", fill=_AMBER, font=cue_font)
                draw.text((44, caption_top + 29), "Budget reached. Model control stops.", fill=_WHITE, font=caption_font)
                draw.text((44, caption_top + 57), "Built-in game AI continues.", fill=_WHITE, font=caption_font)
            elif frame >= first_ring:
                draw.rectangle((24, caption_top + 26, 29, caption_top + 64), fill=_AMBER)
                lead = "Orange rings"
                draw.text((44, caption_top + 31), lead, fill=_AMBER, font=caption_bold)
                draw.text((44 + draw.textlength(lead + " ", font=caption_bold), caption_top + 31), "show AI model control.", fill=_WHITE, font=caption_font)

            frame_image = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
            encoder.stdin.write(frame_image.tobytes())
    finally:
        encoder.stdin.close()
        encoder.wait()
        for decoder in decoders:
            decoder.kill()
    return {"first_ring": first_ring, "callout": callout, "cap": cap, "handoff": handoff, "right_accent": right_accent}
