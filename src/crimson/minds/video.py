"""What a minds video shows on top of the game: who controls each enemy, and what each mind is costing.

`MeterTimeline` reads a run directory (`receipts.jsonl`, `transcript-<mind>.jsonl`, `lease-mind-<n>.json`,
`run.minds.json`) into per-mind call histories. `make_callbacks` returns the two render hooks
`run_replay_render_video` takes: `before_draw` tints each creature by the mind steering it this tick (render
only, through `crimson.render.world.overrides`), and `on_frame` draws the metering strip along the bottom of the
frame: the run's AI cost, a budget bar per mind (overdraft in red past the cap), the latest model calls with
their tokens, latency and cost, and the latest event (a budget cap, an on-chain revocation).
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from grim.color import RGBA
from grim.fonts.small import draw_small_text, measure_small_text_width
from grim.geom import Vec2
from grim.raylib_api import rl, rl_color, rl_rectangle

from ..render.world.overrides import set_creature_tints
from ..sim.commands import CreatureSteerCommand
from .observe import creature_is_live

if TYPE_CHECKING:
    from ..modes.replay_playback_mode import ReplayPlaybackMode

# Body colours for creatures a mind is steering; native creatures keep their own.
MIND_TINTS: tuple[RGBA, ...] = (RGBA(1.0, 0.3, 0.0, 1.0), RGBA(0.1, 0.85, 1.0, 1.0), RGBA(0.75, 0.4, 1.0, 1.0), RGBA(0.5, 1.0, 0.35, 1.0))
MIND_BYTES: tuple[tuple[int, int, int], ...] = ((255, 140, 30), (56, 217, 255), (191, 102, 255), (128, 255, 90))
EVENT_HOLD_TICKS = 150
TICKER_CALLS = 2
STRIP_HEIGHT = 120


def model_label(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("jev"):
        return "Jev"
    if "qwen" in lowered:
        size = next((part for part in lowered.replace("-", " ").split() if part.endswith("b") and part[:-1].replace(".", "").isdigit()), "")
        return f"Qwen {size.upper()}".strip()
    return model


@dataclass(frozen=True)
class Call:
    tick: int
    mind_id: int
    number: int
    tokens: int
    cost: int
    latency_s: float | None
    ok: bool


@dataclass
class MindTrack:
    mind_id: int
    model: str
    budget: int
    governed: bool
    usd_per_mtok: float
    calls: list[Call] = field(default_factory=list)
    cap_tick: int | None = None
    revoke_tick: int | None = None
    _ticks: list[int] = field(default_factory=list)
    _spent: list[int] = field(default_factory=list)
    _tokens: list[int] = field(default_factory=list)

    def finish(self) -> None:
        spent = tokens = 0
        for call in self.calls:
            spent += call.cost
            tokens += call.tokens
            self._ticks.append(call.tick)
            self._spent.append(spent)
            self._tokens.append(tokens)

    def _upto(self, tick: int) -> int:
        return bisect.bisect_right(self._ticks, tick)

    def spent(self, tick: int) -> int:
        n = self._upto(tick)
        return self._spent[n - 1] if n else 0

    def tokens(self, tick: int) -> int:
        n = self._upto(tick)
        return self._tokens[n - 1] if n else 0

    def usd(self, tick: int) -> float:
        return self.tokens(tick) * self.usd_per_mtok / 1_000_000

    def state(self, tick: int) -> tuple[str, tuple[int, int, int]]:
        if self.revoke_tick is not None and tick >= self.revoke_tick:
            return "REVOKED ON CHAIN", (255, 80, 80)
        if self.cap_tick is not None and tick >= self.cap_tick:
            return "BUDGET CAP", (255, 170, 40)
        spent = self.spent(tick)
        if not self.governed and spent > self.budget:
            return f"OVER CAP +{spent - self.budget:,}", (255, 60, 60)
        return "AI ACTIVE", (120, 230, 120)


@dataclass
class MeterTimeline:
    tracks: list[MindTrack]
    events: list[tuple[int, str]]

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> MeterTimeline:
        run_dir = Path(run_dir)
        receipts = [json.loads(line) for line in (run_dir / "receipts.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        meta = json.loads((run_dir / "run.minds.json").read_text(encoding="utf-8"))
        accounts = {int(account["mind_id"]): account for account in meta.get("accounts", [])}
        tracks: list[MindTrack] = []
        events: list[tuple[int, str]] = []
        for mind_id in sorted(accounts):
            lease_path = run_dir / f"lease-mind-{mind_id}.json"
            lease = json.loads(lease_path.read_text(encoding="utf-8")) if lease_path.is_file() else {}
            account = accounts[mind_id]
            budget = int(lease.get("budget_credits") or account.get("budget") or 0)
            track = MindTrack(
                mind_id=mind_id,
                model=model_label(str(lease.get("model") or account.get("model") or "")),
                budget=budget,
                governed=bool(lease.get("governed", meta.get("condition") != "ungoverned")),
                usd_per_mtok=float(lease.get("usd_per_mtok", 0.0)),
            )
            transcript_path = run_dir / f"transcript-{mind_id}.jsonl"
            transcript = (
                [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if transcript_path.is_file()
                else []
            )
            inferences = [row for row in receipts if row["mind_id"] == mind_id and row["kind"] == "inference"]
            for number, row in enumerate(inferences, start=1):
                exchange = transcript[number - 1] if number - 1 < len(transcript) else {}
                latency = exchange.get("latency_s")
                track.calls.append(
                    Call(
                        tick=int(row["tick"]),
                        mind_id=mind_id,
                        number=number,
                        tokens=int(row.get("tokens", 0)),
                        cost=int(row.get("cost", 0)),
                        latency_s=float(latency) if latency else None,
                        ok=row.get("detail") != "failed",
                    ),
                )
            for row in receipts:
                if row["mind_id"] != mind_id:
                    continue
                if row["kind"] == "refusal" and row.get("detail") == "budget_exhausted" and track.cap_tick is None:
                    track.cap_tick = int(row["tick"])
                    events.append((track.cap_tick, f"mind {mind_id} hit its budget cap, game AI takes over"))
                if row["kind"] == "revocation" and track.revoke_tick is None:
                    track.revoke_tick = int(row["tick"])
                    events.append((track.revoke_tick, f"operator revoked mind {mind_id} on chain"))
            if not track.governed and track.calls:
                over = next((call for call in track.calls if sum(c.cost for c in track.calls if c.number <= call.number) > budget), None)
                if over is not None:
                    events.append((over.tick, f"mind {mind_id} passed its {budget} cr cap"))
            track.finish()
            tracks.append(track)
        events.sort()
        return cls(tracks=tracks, events=events)

    def total_usd(self, tick: int) -> float:
        return sum(track.usd(tick) for track in self.tracks)

    def total_tokens(self, tick: int) -> int:
        return sum(track.tokens(tick) for track in self.tracks)

    def recent_calls(self, tick: int, count: int) -> list[Call]:
        calls = [call for track in self.tracks for call in track.calls if call.tick <= tick]
        calls.sort(key=lambda call: (call.tick, call.mind_id))
        return calls[-count:][::-1]

    def event(self, tick: int) -> str | None:
        current = [text for event_tick, text in self.events if event_tick <= tick < event_tick + EVENT_HOLD_TICKS]
        return current[-1] if current else None


def steer_schedule(replay_ticks: list) -> list[dict[int, int]]:
    """For every tick, pool slot -> mind id for the steers in force on it, from the recorded commands alone.

    A steer recorded on tick t with `hold_ticks` h is in force on ticks t..t+h-1 unless a later steer of the same
    slot replaces it. (The session also ends a hold when its creature dies; the renderer only tints live creatures.)
    The session's own `steer_holds` cannot be used: it holds only what carries into the next tick, so it is empty
    on the last tick of every hold.
    """

    active: dict[int, tuple[int, int]] = {}
    schedule: list[dict[int, int]] = []
    for tick, replay_tick in enumerate(replay_ticks):
        for command in replay_tick.commands:
            if isinstance(command, CreatureSteerCommand):
                active[int(command.creature_index)] = (int(command.mind_id), tick + int(command.hold_ticks) - 1)
        active = {slot: (mind, until) for slot, (mind, until) in active.items() if until >= tick}
        schedule.append({slot: mind for slot, (mind, _until) in active.items()})
    return schedule


def steered_slots(mode: ReplayPlaybackMode, schedule: list[dict[int, int]] | None = None) -> dict[int, int]:
    """Pool slot -> mind id for the live creatures a mind steered on the last tick played."""

    tick = mode.tick_index - 1
    if schedule is None:
        schedule = steer_schedule(list(mode.replay.ticks))
    if tick < 0 or tick >= len(schedule):
        return {}
    entries = mode.driver.session.world.creatures.entries
    return {slot: mind for slot, mind in schedule[tick].items() if 0 <= slot < len(entries) and creature_is_live(entries[slot])}


def _usd(value: float) -> str:
    return f"${value:.5f}" if value > 0 else "$0"


def make_callbacks(
    timeline: MeterTimeline, *, width: int = 512, height: int = 512, strip: bool = True,
) -> tuple[Callable[[ReplayPlaybackMode], None], Callable[[ReplayPlaybackMode], None]]:
    schedule: list[dict[int, int]] = []

    def slots(mode: ReplayPlaybackMode) -> dict[int, int]:
        if not schedule:
            schedule.extend(steer_schedule(list(mode.replay.ticks)))
        return steered_slots(mode, schedule)

    def before_draw(mode: ReplayPlaybackMode) -> None:
        set_creature_tints({slot: MIND_TINTS[mind % len(MIND_TINTS)] for slot, mind in slots(mode).items()})

    def on_frame(mode: ReplayPlaybackMode) -> None:
        font = mode.small_font
        tick = max(0, mode.tick_index - 1)
        top = height - STRIP_HEIGHT
        # A ring in the mind's colour round every creature it steers, so ownership reads at any size.
        entries = mode.driver.session.world.creatures.entries
        for slot, mind in slots(mode).items():
            creature = entries[slot]
            screen = mode.runtime.world_to_screen(creature.pos)
            if not (0 <= screen.x <= width and 0 <= screen.y < (top if strip else height)):
                continue
            radius = max(8, int(float(creature.size) * 0.42))
            ring = rl_color(*MIND_BYTES[mind % len(MIND_BYTES)], 220)
            rl.draw_circle_lines(int(screen.x), int(screen.y), radius, ring)
            rl.draw_circle_lines(int(screen.x), int(screen.y), radius + 1, ring)
        if not strip:
            return
        rl.draw_rectangle_rec(rl_rectangle(0, top, width, STRIP_HEIGHT), rl_color(0, 0, 0, 170))

        def text(value: str, x: float, y: float, colour: tuple[int, int, int], *, limit: float | None = None) -> float:
            shown = value
            if limit is not None:
                while shown and measure_small_text_width(font, shown) > limit:
                    shown = shown[:-1]
            draw_small_text(font, shown, Vec2(x, y), rl_color(*colour, 255))
            return measure_small_text_width(font, shown)

        free = timeline.tracks and all(track.usd_per_mtok == 0 for track in timeline.tracks)
        cost = "$0 (self-hosted)" if free else _usd(timeline.total_usd(tick))
        credits = sum(track.spent(tick) for track in timeline.tracks)
        text(f"AI bill so far: {cost} | {timeline.total_tokens(tick):,} tokens | {credits:,} cr", 8, top + 2, (255, 255, 255), limit=width - 16)

        y = top + 20
        for track in timeline.tracks:
            colour = MIND_BYTES[track.mind_id % len(MIND_BYTES)]
            spent = track.spent(tick)
            rl.draw_rectangle_rec(rl_rectangle(8, y + 3, 10, 10), rl_color(*colour, 255))
            label = f"m{track.mind_id} {track.model}  spent {spent:,}/{track.budget:,} cr"
            used = text(label, 24, y, (235, 235, 235))
            state, state_colour = track.state(tick)
            state_width = measure_small_text_width(font, state)
            text(state, max(24 + used + 10, width - 8 - state_width), y, state_colour)
            bar_y = y + 17
            bar_w = width - 16
            rl.draw_rectangle_rec(rl_rectangle(8, bar_y, bar_w, 6), rl_color(60, 60, 60, 230))
            budget = max(1, track.budget)
            if track.governed or spent <= budget:
                left = max(0.0, (budget - spent) / budget)
                bar_colour = (90, 210, 90) if left > 0.5 else (240, 180, 40) if left > 0.15 else (240, 70, 60)
                rl.draw_rectangle_rec(rl_rectangle(8, bar_y, bar_w * left, 6), rl_color(*bar_colour, 255))
            else:
                # Past the cap: the whole bar is gone, and an overdraft bar grows from the cap line in red,
                # scaled so ten budgets fill the width.
                over = min(1.0, (spent - budget) / (budget * 10))
                rl.draw_rectangle_rec(rl_rectangle(8, bar_y, max(2.0, bar_w * over), 6), rl_color(240, 40, 40, 255))
            y += 26

        for call in timeline.recent_calls(tick, TICKER_CALLS):
            track = timeline.tracks[call.mind_id]
            latency = f" {call.latency_s:.2f}s" if call.latency_s else ""
            price = "" if track.usd_per_mtok == 0 else f" {_usd(call.tokens * track.usd_per_mtok / 1_000_000)}"
            line = f"{track.model} call #{call.number} m{call.mind_id}: {call.tokens:,} tok{latency} {call.cost} cr{price}"
            text(line, 8, y, MIND_BYTES[call.mind_id % len(MIND_BYTES)], limit=width - 16)
            y += 16

        event = timeline.event(tick)
        if event:
            text(event, 8, height - 18, (255, 220, 80), limit=width - 16)

    return before_draw, on_frame
