"""The player in a minds run: an aim bot, so a headless run has someone to shoot the horde.

It aims the mouse at the nearest live creature, holds fire, and walks away from anything that gets close,
otherwise drifting back to the middle of the arena. It is deliberately simple: the experiment is about
the creatures' minds, and the same pilot plays every condition.
"""

from __future__ import annotations

from grim.geom import Vec2

from ..aim_schemes import AimScheme
from ..movement_controls import MovementControlType
from ..sim.input import PlayerInput
from ..sim.state_types import TERRAIN_SIZE
from ..sim.world_state import WorldState
from .observe import creature_is_live


class AimBot:
    def __init__(self, *, keep_distance: float = 150.0, drift: float = 0.35) -> None:
        self._keep_distance = float(keep_distance)
        self._drift = float(drift)

    def input(self, world: WorldState) -> PlayerInput:
        player = world.players[0]
        nearest: Vec2 | None = None
        nearest_dist_sq = 0.0
        for creature in world.creatures.entries:
            if not creature_is_live(creature):
                continue
            dist_sq = (creature.pos - player.pos).length_sq()
            if nearest is None or dist_sq < nearest_dist_sq:
                nearest, nearest_dist_sq = creature.pos, dist_sq
        aim = nearest if nearest is not None else Vec2(player.pos.x + 64.0, player.pos.y)
        center = Vec2(TERRAIN_SIZE * 0.5, TERRAIN_SIZE * 0.5)
        if nearest is not None and nearest_dist_sq < self._keep_distance * self._keep_distance:
            away = player.pos - nearest
            move = away.normalized() if away.length_sq() > 0.0 else Vec2(1.0, 0.0)
        else:
            to_center = center - player.pos
            move = to_center.normalized() * self._drift if to_center.length_sq() > 1.0 else Vec2()
        return PlayerInput(
            move_mode=MovementControlType.DUAL_ACTION_PAD,
            aim_scheme=AimScheme.MOUSE,
            move=move,
            aim=aim,
            fire_down=True,
            fire_pressed=False,
            move_forward_down=False,
            move_backward_down=False,
            turn_left_down=False,
            turn_right_down=False,
        )
