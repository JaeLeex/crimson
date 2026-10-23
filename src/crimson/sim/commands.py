from __future__ import annotations

from typing import Annotated

import msgspec

type TypoChar = Annotated[str, msgspec.Meta(min_length=1, max_length=1)]


class PerkMenuOpenCommand(msgspec.Struct, tag="perk_menu_open", frozen=True, forbid_unknown_fields=True):
    player_index: int


class PerkPickCommand(msgspec.Struct, tag="perk_pick", frozen=True, forbid_unknown_fields=True):
    player_index: int
    choice_index: int


class TypoCharCommand(msgspec.Struct, tag="typo_char", frozen=True, forbid_unknown_fields=True):
    player_index: int
    ch: TypoChar


class TypoBackspaceCommand(msgspec.Struct, tag="typo_backspace", frozen=True, forbid_unknown_fields=True):
    player_index: int


class TypoSubmitCommand(msgspec.Struct, tag="typo_submit", frozen=True, forbid_unknown_fields=True):
    player_index: int


# The longest a steer may hold a creature's target: ten seconds of ticks.
STEER_HOLD_MAX_TICKS = 600


class CreatureSteerCommand(msgspec.Struct, tag="creature_steer", frozen=True, forbid_unknown_fields=True):
    """A mind's target for one creature on this tick (the minds rules profile, `MINDS_RULES`).

    The creature runs its native AI step first; only the target it then turns toward is replaced
    (`creature_apply_steer`). Movement, cooldowns and every RNG draw stay native. `mind_id` names
    the controller for the audit trail; `target_x`/`target_y` are canonical f32 arena coordinates.
    `hold_ticks` keeps the target for that many ticks (this one included) unless a later steer of the
    slot replaces it or the creature dies; a mind asked every N ticks records one steer, not N.
    """

    creature_index: int
    mind_id: int
    target_x: float
    target_y: float
    hold_ticks: int


type GameCommand = (
    PerkMenuOpenCommand
    | PerkPickCommand
    | TypoCharCommand
    | TypoBackspaceCommand
    | TypoSubmitCommand
    | CreatureSteerCommand
)
