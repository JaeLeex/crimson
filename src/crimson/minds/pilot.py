"""The player in a minds run: an aim bot, so a headless run has someone to shoot the horde.

It aims the mouse at the nearest live creature, fires, and walks away from anything that gets close,
otherwise drifting back to the middle of the arena. Its skill is a dial: a perfect bot takes no damage
from any horde, so the default aim lags and fire comes in bursts, which leaves room for a flank to land.
The same pilot plays every condition of a comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from grim.geom import Vec2

from ..aim_schemes import AimScheme
from ..movement_controls import MovementControlType
from ..sim.input import PlayerInput
from ..sim.state_types import TERRAIN_SIZE
from ..sim.world_state import WorldState
from .observe import creature_is_live


@dataclass(frozen=True)
class PilotSkill:
    # The aim point moves at most this many units per tick toward the target (0 = snap).
    aim_lag: float = 40.0
    # Fire is held for `fire_on` ticks, then released for `fire_off` ticks.
    fire_on: int = 20
    fire_off: int = 20
    # Walk away from a creature closer than this.
    keep_distance: float = 150.0
    # Stick magnitude when drifting back to the middle.
    drift: float = 0.35


class AimBot:
    def __init__(self, skill: PilotSkill | None = None) -> None:
        self._skill = PilotSkill() if skill is None else skill
        self._aim: Vec2 | None = None
        self._tick = 0

    def input(self, world: WorldState) -> PlayerInput:
        skill = self._skill
        player = world.players[0]
        nearest: Vec2 | None = None
        nearest_dist_sq = 0.0
        for creature in world.creatures.entries:
            if not creature_is_live(creature):
                continue
            dist_sq = (creature.pos - player.pos).length_sq()
            if nearest is None or dist_sq < nearest_dist_sq:
                nearest, nearest_dist_sq = creature.pos, dist_sq
        wanted = nearest if nearest is not None else Vec2(player.pos.x + 64.0, player.pos.y)
        if self._aim is None or skill.aim_lag <= 0.0:
            aim = wanted
        else:
            step = wanted - self._aim
            length = step.length()
            aim = wanted if length <= skill.aim_lag else self._aim + step * (skill.aim_lag / length)
        self._aim = aim

        center = Vec2(TERRAIN_SIZE * 0.5, TERRAIN_SIZE * 0.5)
        if nearest is not None and nearest_dist_sq < skill.keep_distance * skill.keep_distance:
            away = player.pos - nearest
            move = away.normalized() if away.length_sq() > 0.0 else Vec2(1.0, 0.0)
        else:
            to_center = center - player.pos
            move = to_center.normalized() * skill.drift if to_center.length_sq() > 1.0 else Vec2()

        period = max(1, skill.fire_on + skill.fire_off)
        fire = (self._tick % period) < skill.fire_on
        self._tick += 1
        return PlayerInput(
            move_mode=MovementControlType.DUAL_ACTION_PAD,
            aim_scheme=AimScheme.MOUSE,
            move=move,
            aim=aim,
            fire_down=fire,
            fire_pressed=False,
            move_forward_down=False,
            move_backward_down=False,
            turn_left_down=False,
            turn_right_down=False,
        )
