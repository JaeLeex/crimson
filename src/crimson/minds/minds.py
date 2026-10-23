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
    """Sends its creatures around the player's aim: half to each side of the firing line, the rest behind.

    Which side a creature takes is drawn from the mind's own seeded stream, so two runs with different
    mind seeds differ while each replays exactly.
    """

    name = "flank"
    model = "heuristic"

    def __init__(self, seed: int = 0, *, ring: float = 140.0) -> None:
        self._rng = random.Random(int(seed))
        self._ring = float(ring)

    def _target(self, observation: Observation, creature: CreatureView) -> Steer:
        arena = float(observation.arena)
        px, py = observation.player_x, observation.player_y
        aim_dx, aim_dy = observation.player_aim_x - px, observation.player_aim_y - py
        norm = math.hypot(aim_dx, aim_dy)
        if norm < 1e-6:
            aim_dx, aim_dy, norm = 1.0, 0.0, 1.0
        aim_dx, aim_dy = aim_dx / norm, aim_dy / norm
        roll = self._rng.random()
        if roll < 0.4:
            side_x, side_y = -aim_dy, aim_dx
        elif roll < 0.8:
            side_x, side_y = aim_dy, -aim_dx
        else:
            side_x, side_y = -aim_dx, -aim_dy
        close = math.hypot(creature.x - px, creature.y - py) < self._ring * 0.6
        ring = 0.0 if close else self._ring
        return Steer(
            creature_index=creature.index,
            target_x=clamp_arena(px + side_x * ring, arena),
            target_y=clamp_arena(py + side_y * ring, arena),
        )

    def decide(self, observation: Observation) -> Decision:
        steers = tuple(self._target(observation, creature) for creature in observation.owned)
        prompt_tokens, completion_tokens = _standin_tokens(observation, len(steers))
        return Decision(steers=steers, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
