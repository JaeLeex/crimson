"""What a mind is told about the world when it decides."""

from __future__ import annotations

from collections.abc import Collection

import msgspec

from ..creatures.lifecycle import creature_lifecycle_is_alive
from ..creatures.runtime import CreatureState
from ..sim.state_types import TERRAIN_SIZE
from ..sim.world_state import WorldState


class CreatureView(msgspec.Struct, frozen=True):
    index: int
    type_id: int
    x: float
    y: float
    hp: float
    max_hp: float
    heading: float
    ai_mode: int
    size: float


class Observation(msgspec.Struct, frozen=True):
    """One mind's view at one tick: the player, the creatures it owns, and the nearest others."""

    tick: int
    elapsed_ms: int
    arena: float
    player_x: float
    player_y: float
    player_health: float
    player_aim_x: float
    player_aim_y: float
    player_weapon: int
    kills: int
    owned: tuple[CreatureView, ...]
    others: tuple[CreatureView, ...]


def creature_is_live(creature: CreatureState) -> bool:
    """Whether a pool slot holds a creature a steer may address (the session's own rule)."""

    return bool(creature.active) and creature.hp > 0.0 and creature_lifecycle_is_alive(creature.death_timer)


def creature_view(index: int, creature: CreatureState) -> CreatureView:
    return CreatureView(
        index=int(index),
        type_id=int(creature.type_id),
        x=float(creature.pos.x),
        y=float(creature.pos.y),
        hp=float(creature.hp),
        max_hp=float(creature.max_hp),
        heading=float(creature.heading),
        ai_mode=int(creature.ai_mode),
        size=float(creature.size),
    )


def live_creature_indices(world: WorldState) -> list[int]:
    return [index for index, creature in enumerate(world.creatures.entries) if creature_is_live(creature)]


def observe(
    world: WorldState,
    *,
    tick: int,
    elapsed_ms: float,
    owned: Collection[int],
    others_limit: int = 12,
) -> Observation:
    """The observation for a mind that owns the slots in `owned`; `others` are the live creatures nearest the player."""

    player = world.players[0]
    entries = world.creatures.entries
    owned_set = {int(index) for index in owned}
    owned_views = tuple(creature_view(index, entries[index]) for index in sorted(owned_set) if creature_is_live(entries[index]))
    others = [
        index for index, creature in enumerate(entries) if index not in owned_set and creature_is_live(creature)
    ]
    others.sort(key=lambda index: (entries[index].pos - player.pos).length_sq())
    return Observation(
        tick=int(tick),
        elapsed_ms=int(elapsed_ms),
        arena=float(TERRAIN_SIZE),
        player_x=float(player.pos.x),
        player_y=float(player.pos.y),
        player_health=float(player.health),
        player_aim_x=float(player.aim.x),
        player_aim_y=float(player.aim.y),
        player_weapon=int(player.weapon.weapon_id),
        kills=int(world.creatures.kill_count),
        owned=owned_views,
        others=tuple(creature_view(index, entries[index]) for index in others[: max(0, int(others_limit))]),
    )
