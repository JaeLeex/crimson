"""Compose minds videos at full resolution: game footage on top, large readable meters under it (Pillow + ffmpeg).

The in-game strip (`video.make_callbacks(strip=True)`) is drawn in the game's 16 px bitmap font inside a 512 px
frame, which is too small once two or four games share a screen. Here each game is rendered with only the enemy
rings and tints, then every output frame is laid out with Pillow: a title band, the games, and under each game a
meter block (status, a usage bar against the allowance, and the AI cost so far). One rendered frame is one game
tick (60 per second), so the meters are read off the run's receipts at that tick.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
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

