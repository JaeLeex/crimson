"""Title and end cards for a minds video (Pillow), sized to the grid they frame."""

from __future__ import annotations

import json
from pathlib import Path

from .video import MIND_BYTES, model_label

_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
_BG = (14, 16, 20)


def _font(path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def title_card(path: Path, *, width: int, height: int, title: str, subtitle: str) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    big, mid, small = _font(_BOLD, max(28, width // 26)), _font(_REGULAR, max(20, width // 40)), _font(_REGULAR, max(18, width // 46))
    y = height // 4
    for line in _wrap(draw, title, big, width - 120):
        draw.text((60, y), line, fill=(255, 255, 255), font=big)
        y += int(big.size * 1.25)
    y += 16
    for line in _wrap(draw, subtitle, mid, width - 120):
        draw.text((60, y), line, fill=(200, 205, 215), font=mid)
        y += int(mid.size * 1.35)
    y += 40
    legend = [
        (MIND_BYTES[0], "orange enemy = steered by AI mind 0"),
        (MIND_BYTES[1], "cyan enemy = steered by AI mind 1"),
        ((170, 170, 170), "normal colour = the game's own AI (no mind, or its mind was cut off)"),
        ((90, 210, 90), "bars = credits left on each mind's Nunchi chain budget; red past the cap = overdraft"),
        ((255, 255, 255), "ticker = every AI call as it happens: model, tokens, latency, credits, dollars"),
    ]
    for colour, label in legend:
        draw.rectangle((60, y + 4, 60 + small.size, y + 4 + small.size), fill=colour)
        for index, line in enumerate(_wrap(draw, label, small, width - 140)):
            draw.text((60 + small.size + 14, y + index * int(small.size * 1.3)), line, fill=(225, 228, 235), font=small)
        y += int(small.size * 1.3) * max(1, len(_wrap(draw, label, small, width - 140))) + 10
    image.save(path)


def _block_lines(run_dir: Path, name: str, audit: tuple[int, int] | None) -> list[tuple[str, tuple[int, int, int], bool]]:
    """(text, colour, bold) lines summarising one run's settlement."""

    meta = json.loads((run_dir / "run.minds.json").read_text(encoding="utf-8"))
    settlement = meta.get("settlement") or {}
    lines: list[tuple[str, tuple[int, int, int], bool]] = [(name, (255, 255, 255), True)]
    tokens = 0
    usd_micros = 0
    for entry in settlement.get("minds", []):
        mind_id = int(entry["mind"])
        lease = json.loads((run_dir / entry["admission"]["lease_file"]).read_text(encoding="utf-8"))
        model = model_label(str(lease.get("model", "")))
        settled = entry["settlement"]
        colour = MIND_BYTES[mind_id % len(MIND_BYTES)]
        debit = next((tx for tx in settled["txs"] if tx.get("op") == "debit"), {})
        if settled["settled"]:
            block = str(debit.get("status", "")).replace("finalized:", "")
            lines.append((f"m{mind_id} {model}: chain debited {int(settled['spent']):,} cr (block {block})", colour, False))
            lines.append((f"   {int(settled['balance_before']):,} -> {int(settled['balance_after']):,} cr left · tx {str(debit.get('tx', ''))[:10]}", (190, 195, 205), False))
        else:
            lines.append((f"m{mind_id} {model}: chain REFUSED {int(settled['spent']):,} cr", (255, 90, 80), False))
            lines.append((f"   only {int(lease.get('budget_credits', 0)):,} issued · balance kept at {int(settled['balance_after']):,}", (190, 195, 205), False))
        usd_micros += int(settled.get("usage_cost_usd_micros_total", 0))
    for account in meta.get("accounts", []):
        tokens += int(account.get("tokens", 0))
    for event in settlement.get("events", []):
        block = str(event.get("tx", {}).get("status", "")).replace("finalized:", "")
        lines.append((f"m{event['mind']} revoked on chain at {int(event['tick']) / 60:.0f} s (block {block})", (255, 120, 120), False))
    cost = f"${usd_micros / 1_000_000:.4f}" if usd_micros else "$0 API (self-hosted)"
    lines.append((f"AI metered: {tokens:,} tokens · {cost}", (255, 225, 120), False))
    if audit is not None:
        passed, total = audit
        lines.append((f"audit on chain: {passed}/{total} checks pass", (120, 230, 120) if passed == total else (255, 90, 80), True))
    return lines


def end_card(path: Path, *, width: int, height: int, tiles: list[tuple[Path, str, tuple[int, int] | None]]) -> None:
    """One block per tile, in the same layout as the grid (2 side by side, or 2x2)."""

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    columns = 2
    rows = 1 if len(tiles) == 2 else 2
    cell_w, cell_h = width // columns, height // rows
    head, body = _font(_BOLD, max(20, cell_w // 22)), _font(_REGULAR, max(15, cell_w // 32))
    for index, (run_dir, name, audit) in enumerate(tiles):
        x0, y0 = (index % columns) * cell_w + 24, (index // columns) * cell_h + 28
        y = y0
        for text, colour, bold in _block_lines(run_dir, name, audit):
            font = head if bold else body
            for line in _wrap(draw, text, font, cell_w - 48):
                draw.text((x0, y), line, fill=colour, font=font)
                y += int(font.size * 1.35)
            if bold and y == y0 + int(head.size * 1.35):
                y += 6
        if index % columns == 0:
            draw.line(((index % columns + 1) * cell_w, (index // columns) * cell_h + 16, (index % columns + 1) * cell_w, (index // columns + 1) * cell_h - 16), fill=(60, 64, 72), width=2)
    if rows == 2:
        draw.line((16, cell_h, width - 16, cell_h), fill=(60, 64, 72), width=2)
    image.save(path)
