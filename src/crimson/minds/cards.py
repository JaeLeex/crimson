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
        (MIND_BYTES[0], "orange: enemy steered by AI mind 0"),
        (MIND_BYTES[1], "cyan: enemy steered by AI mind 1"),
        ((170, 170, 170), "normal colour: the game's own AI"),
        ((90, 210, 90), "bars: credits left in each mind's budget"),
    ]
    for colour, label in legend:
        draw.rectangle((60, y + 4, 60 + small.size, y + 4 + small.size), fill=colour)
        for index, line in enumerate(_wrap(draw, label, small, width - 140)):
            draw.text((60 + small.size + 14, y + index * int(small.size * 1.3)), line, fill=(225, 228, 235), font=small)
        y += int(small.size * 1.3) * max(1, len(_wrap(draw, label, small, width - 140))) + 10
    image.save(path)


class _Row:
    def __init__(self, label: str, calls: int, tokens: int, usd: float | None, credits: int, colour: tuple[int, int, int], bold: bool = False) -> None:
        self.label, self.calls, self.tokens, self.usd, self.credits, self.colour, self.bold = label, calls, tokens, usd, credits, colour, bold


def _money(usd: float | None) -> str:
    return "$0" if usd is None or usd == 0 else f"${usd:.4f}"


def _bill(run_dir: Path) -> tuple[list[_Row], list[tuple[str, tuple[int, int, int]]]]:
    """The bill's rows (one per mind, then the total) and the lines under it (price, what the chain did)."""

    meta = json.loads((run_dir / "run.minds.json").read_text(encoding="utf-8"))
    settlement = meta.get("settlement") or {}
    rows: list[_Row] = []
    notes: list[tuple[str, tuple[int, int, int]]] = []
    issued = 0
    billed = 0
    refused = False
    blocks: list[str] = []
    prices: dict[str, float] = {}
    self_hosted = False
    for entry in settlement.get("minds", []):
        mind_id = int(entry["mind"])
        lease = json.loads((run_dir / entry["admission"]["lease_file"]).read_text(encoding="utf-8"))
        account = next(a for a in meta.get("accounts", []) if int(a["mind_id"]) == mind_id)
        model = model_label(str(lease.get("model", "")))
        usd_per_mtok = float(lease.get("usd_per_mtok", 0.0))
        tokens = int(account.get("tokens", 0))
        usd = tokens * usd_per_mtok / 1_000_000 if usd_per_mtok else None
        self_hosted = self_hosted or not usd_per_mtok
        prices[model] = usd_per_mtok
        rows.append(_Row(f"mind {mind_id} ({model})", int(account.get("inferences", 0)), tokens, usd, int(account.get("spent", 0)), MIND_BYTES[mind_id % len(MIND_BYTES)]))
        settled = entry["settlement"]
        issued += int(settled["balance_before"])
        billed += int(settled["spent"])
        refused = refused or not settled["settled"]
        debit = next((tx for tx in settled["txs"] if tx.get("op") == "debit" and tx.get("status")), None)
        if debit:
            blocks.append(str(debit["status"]).replace("finalized:", ""))
    total_usd = sum(row.usd or 0.0 for row in rows)
    rows.append(_Row("Total", sum(r.calls for r in rows), sum(r.tokens for r in rows), total_usd if not self_hosted else None, billed, (255, 255, 255), bold=True))
    for model, usd_per_mtok in prices.items():
        if usd_per_mtok:
            notes.append((f"{model}: ${usd_per_mtok:g} per million tokens", (190, 195, 205)))
        else:
            notes.append((f"{model}: runs on our own machine, no API charge", (190, 195, 205)))
    for event in settlement.get("events", []):
        notes.append((f"mind {event['mind']} cut off on chain at 0:{int(event['tick']) // 60:02d}", (255, 120, 120)))
    if refused:
        notes.append((f"Nunchi refused the bill: {billed:,} credits against {issued:,} issued. Nothing was charged.", (255, 90, 80)))
    else:
        where = f" (block {blocks[-1]})" if blocks else ""
        notes.append((f"Nunchi charged {billed:,} of {issued:,} credits{where}.", (120, 230, 120)))
    return rows, notes


def end_card(path: Path, *, width: int, height: int, tiles: list[tuple[Path, str]]) -> None:
    """One itemised AI bill per game, in the same layout as the grid (2 side by side, or 2x2)."""

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    columns = 2
    rows_n = 1 if len(tiles) == 2 else 2
    cell_w, cell_h = width // columns, height // rows_n
    head = _font(_BOLD, max(20, cell_w // 22))
    body = _font(_REGULAR, max(14, cell_w // 34))
    bold = _font(_BOLD, max(14, cell_w // 34))
    for index, (run_dir, name) in enumerate(tiles):
        x0, y = (index % columns) * cell_w + 24, (index // columns) * cell_h + 28
        draw.text((x0, y), f"AI bill: {name}", fill=(255, 255, 255), font=head)
        y += int(head.size * 1.6)
        bill_rows, notes = _bill(run_dir)
        # Columns: item, calls, tokens, cost, credits (right-aligned numbers).
        right = [x0 + int(cell_w * f) for f in (0.50, 0.66, 0.80, 0.93)]
        headers = ("calls", "tokens", "cost", "credits")
        for x, label in zip(right, headers, strict=True):
            draw.text((x - draw.textlength(label, font=body), y), label, fill=(150, 155, 165), font=body)
        y += int(body.size * 1.5)
        for row in bill_rows:
            font = bold if row.bold else body
            if row.bold:
                draw.line((x0, y - 4, x0 + int(cell_w * 0.93) - 0, y - 4), fill=(80, 84, 92), width=1)
            draw.text((x0, y), row.label, fill=row.colour, font=font)
            for x, value in zip(right, (f"{row.calls:,}", f"{row.tokens:,}", _money(row.usd), f"{row.credits:,}"), strict=True):
                draw.text((x - draw.textlength(value, font=font), y), value, fill=row.colour, font=font)
            y += int(font.size * 1.5)
        y += 10
        for text, colour in notes:
            for line in _wrap(draw, text, body, cell_w - 48):
                draw.text((x0, y), line, fill=colour, font=body)
                y += int(body.size * 1.4)
        if index % columns == 0:
            draw.line((cell_w, (index // columns) * cell_h + 16, cell_w, (index // columns + 1) * cell_h - 16), fill=(60, 64, 72), width=2)
    if rows_n == 2:
        draw.line((16, cell_h, width - 16, cell_h), fill=(60, 64, 72), width=2)
    image.save(path)
