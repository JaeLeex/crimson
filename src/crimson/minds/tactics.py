"""A small vocabulary of moves, so a mind can say what it wants instead of inventing coordinates.

Each move is a point relative to the player and the direction they aim. The driver turns a move into a
steer; a model picking one of six words at a second per call reads better than one inventing numbers.
"""

from __future__ import annotations

import math

from .minds import Steer, clamp_arena
from .observe import CreatureView, Observation

MOVES = ("flank_left", "flank_right", "behind", "rush", "hold", "spread")
RING = 150.0


def aim_direction(observation: Observation) -> tuple[float, float]:
    """Unit vector from the player toward the aim point; east when the aim sits on the player."""

    dx = observation.player_aim_x - observation.player_x
    dy = observation.player_aim_y - observation.player_y
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return 1.0, 0.0
    return dx / norm, dy / norm


def move_target(move: str, observation: Observation, creature: CreatureView, *, ring: float = RING) -> tuple[float, float]:
    """Where `move` sends `creature`, as arena coordinates (unclamped)."""

    px, py = observation.player_x, observation.player_y
    ax, ay = aim_direction(observation)
    # Screen y points down, so the player's left when facing the aim is (ay, −ax): north when aiming east.
    left = (ay, -ax)
    right = (-ay, ax)
    back = (-ax, -ay)
    dist = math.hypot(creature.x - px, creature.y - py)
    match move:
        case "flank_left":
            r = 0.0 if dist < ring * 0.5 else ring
            return px + left[0] * r, py + left[1] * r
        case "flank_right":
            r = 0.0 if dist < ring * 0.5 else ring
            return px + right[0] * r, py + right[1] * r
        case "behind":
            r = 0.0 if dist < ring * 0.5 else ring
            return px + back[0] * r, py + back[1] * r
        case "rush":
            return px, py
        case "hold":
            return creature.x, creature.y
        case "spread":
            # Step away from the player's line of fire: whichever side the creature already leans to.
            side = left if (creature.x - px) * left[0] + (creature.y - py) * left[1] >= 0.0 else right
            return creature.x + side[0] * ring, creature.y + side[1] * ring
        case _:
            raise ValueError(f"unknown move {move!r}")


def steer_for_move(move: str, observation: Observation, creature: CreatureView, *, ring: float = RING) -> Steer:
    x, y = move_target(move, observation, creature, ring=ring)
    arena = float(observation.arena)
    return Steer(creature_index=creature.index, target_x=clamp_arena(x, arena), target_y=clamp_arena(y, arena))
