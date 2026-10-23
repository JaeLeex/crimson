"""Dense scenes for minds runs: custom quests that put a crowd on the field at once.

Survival opens with a trickle, which no tactic can make interesting. These recipes open with a crowd already
closing in, then more waves. Two shapes matter: a **ring** around the player, where the native chase already
attacks from every side, and an **edge** wave, where the native chase arrives as a column down the player's
line of fire and a horde that splits round the sides can do what the column cannot. They compile with the
quest recipe compiler (`crimson.quests.dsl`), so they play as ordinary custom quests.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..quests.custom import CustomQuest


@dataclass(frozen=True)
class ArenaPreset:
    name: str
    weapon: str
    # (trigger seconds, side or "ring", count) per wave.
    waves: tuple[tuple[int, str, int], ...]
    spacing: int = 36
    outside: int = 96


PRESETS: dict[str, ArenaPreset] = {
    # Waves walk in from one edge at a time: the shape where coordination can beat the column.
    "edge": ArenaPreset(
        name="Minds arena (edge)",
        weapon="assault_rifle",
        waves=((0, "west", 18), (14, "east", 22), (28, "north", 26), (42, "south", 30)),
    ),
    # Rings around the player: every side at once, the native AI's best case.
    "ring": ArenaPreset(
        name="Minds arena (ring)",
        weapon="assault_rifle",
        waves=((0, "ring", 18), (14, "ring", 22), (28, "ring", 26), (42, "ring", 30)),
    ),
}


def arena_recipe(preset: ArenaPreset) -> str:
    lines = [
        f'quest "{preset.name}" version 1',
        'author "crimson minds"',
        f"weapon {preset.weapon}",
        "fixed_weapon true",
        "no_bonuses [nuke, freeze, shield, energizer, fireblast, shock_chain, reflex_boost, medikit]",
        "",
    ]
    for seconds, side, count in preset.waves:
        lines.append(f"at {seconds}s:")
        if side == "ring":
            lines.append(f"    place alien.small_gray on ring(center, 440, {count}, start={seconds % 7}deg)")
        else:
            lines.append(f'    place alien.small_gray on edge_line("{side}", {count}, {preset.spacing}, outside={preset.outside})')
    return "\n".join(lines) + "\n"


def arena_quest(preset: str = "edge") -> CustomQuest:
    """The dense arena quest for `preset` (`edge` or `ring`); the recipes draw nothing random."""

    from ..quests.dsl import build_custom_quest

    try:
        chosen = PRESETS[preset]
    except KeyError as exc:
        raise ValueError(f"unknown arena preset {preset!r}; use one of {', '.join(PRESETS)}") from exc
    return build_custom_quest(arena_recipe(chosen), seed=0)
