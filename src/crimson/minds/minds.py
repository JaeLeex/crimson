"""Minds that need no model: a baseline that abstains, a seeded random one, and a flanking heuristic.

Each reports a token count the way a hosted model would, so the ledger can charge it; the counts are
stand-ins (a fixed prompt overhead plus a per-creature share), the actions are what matters.
"""

from __future__ import annotations

import math
import random
from typing import Protocol

import msgspec

from ..math_parity import f32
from .observe import CreatureView, Observation

# Stand-in token accounting for the model-free minds.
PROMPT_TOKENS_BASE = 60
PROMPT_TOKENS_PER_CREATURE = 6
COMPLETION_TOKENS_PER_STEER = 10


class Steer(msgspec.Struct, frozen=True):
    creature_index: int
    target_x: float
    target_y: float


class Decision(msgspec.Struct, frozen=True):
    """One inference's outcome: the steers it proposes and what it cost in tokens."""

    steers: tuple[Steer, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # False when the mind could not decide (a model error); nothing is charged for it.
    ok: bool = True
    note: str = ""


class Mind(Protocol):
    name: str
    model: str

    def decide(self, observation: Observation) -> Decision: ...


def clamp_arena(value: float, arena: float) -> float:
    """A canonical f32 inside the arena, as the steer command requires."""

    return f32(min(max(float(value), 0.0), float(arena)))


def _standin_tokens(observation: Observation, steers: int) -> tuple[int, int]:
    creatures = len(observation.owned) + len(observation.others)
    return PROMPT_TOKENS_BASE + PROMPT_TOKENS_PER_CREATURE * creatures, COMPLETION_TOKENS_PER_STEER * steers


class IdleMind:
    """Never steers: the creatures keep their native AI. The scripted baseline."""

    name = "idle"
    model = "none"

    def decide(self, observation: Observation) -> Decision:
        prompt_tokens, completion_tokens = _standin_tokens(observation, 0)
        return Decision(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


class RandomMind:
    """Steers every owned creature to a uniformly random arena point, from its own seeded stream."""

    name = "random"
    model = "random"

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(int(seed))

    def decide(self, observation: Observation) -> Decision:
        arena = float(observation.arena)
        steers = tuple(
            Steer(
                creature_index=creature.index,
                target_x=clamp_arena(self._rng.uniform(0.0, arena), arena),
                target_y=clamp_arena(self._rng.uniform(0.0, arena), arena),
            )
            for creature in observation.owned
        )
        prompt_tokens, completion_tokens = _standin_tokens(observation, len(steers))
        return Decision(steers=steers, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


class FlankMind:
    """Sends its creatures round the player's aim with the tactics vocabulary (`crimson.minds.tactics`).

    Each creature is dealt a side once (left, right or behind) from the mind's own seeded stream and keeps it, so
    the horde splits instead of queueing on the firing line; a creature already close rushes. Two runs with
    different mind seeds differ while each replays exactly.
    """

    name = "flank"
    model = "heuristic"

    def __init__(self, seed: int = 0, *, ring: float = 150.0) -> None:
        self._rng = random.Random(int(seed))
        self._ring = float(ring)
        self._sides: dict[int, str] = {}

    def _target(self, observation: Observation, creature: CreatureView) -> Steer:
        from .tactics import steer_for_move

        side = self._sides.get(creature.index)
        if side is None:
            side = self._rng.choice(("flank_left", "flank_right", "behind"))
            self._sides[creature.index] = side
        dist = math.hypot(creature.x - observation.player_x, creature.y - observation.player_y)
        move = "rush" if dist < self._ring * 0.5 else side
        return steer_for_move(move, observation, creature, ring=self._ring)

    def decide(self, observation: Observation) -> Decision:
        steers = tuple(self._target(observation, creature) for creature in observation.owned)
        prompt_tokens, completion_tokens = _standin_tokens(observation, len(steers))
        return Decision(steers=steers, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


class SurroundMind:
    """Encircles, then collapses: each creature takes an evenly spaced slot on a ring round the player and, once
    enough of them are in place, all rush at once. The slot order is dealt from the mind's own seeded stream.

    Against a pilot that kites away from the nearest creature, a column can be outrun and a ring cannot.
    """

    name = "surround"
    model = "heuristic"

    def __init__(self, seed: int = 0, *, ring: float = 170.0, collapse_share: float = 0.6) -> None:
        self._rng = random.Random(int(seed))
        self._ring = float(ring)
        self._collapse_share = float(collapse_share)
        self._slots: dict[int, float] = {}

    def decide(self, observation: Observation) -> Decision:
        owned = observation.owned
        if not owned:
            prompt_tokens, completion_tokens = _standin_tokens(observation, 0)
            return Decision(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        for creature in owned:
            if creature.index not in self._slots:
                self._slots[creature.index] = self._rng.random() * math.tau
        px, py = observation.player_x, observation.player_y
        arena = float(observation.arena)
        in_place = sum(1 for c in owned if math.hypot(c.x - px, c.y - py) <= self._ring * 1.4)
        collapse = in_place >= max(1, int(math.ceil(len(owned) * self._collapse_share)))
        steers = []
        for creature in owned:
            if collapse:
                steers.append(Steer(creature_index=creature.index, target_x=clamp_arena(px, arena), target_y=clamp_arena(py, arena)))
                continue
            angle = self._slots[creature.index]
            steers.append(
                Steer(
                    creature_index=creature.index,
                    target_x=clamp_arena(px + math.cos(angle) * self._ring, arena),
                    target_y=clamp_arena(py + math.sin(angle) * self._ring, arena),
                ),
            )
        prompt_tokens, completion_tokens = _standin_tokens(observation, len(steers))
        return Decision(steers=tuple(steers), prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
