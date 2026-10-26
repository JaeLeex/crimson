"""Render-only overrides: colours a viewer asks for that the simulation never sees.

A replay renderer (`crimson minds grid`) uses this to tint each creature by the mind steering it. The map is
empty unless a renderer fills it, so live play and verification are untouched, and the simulation's own
`CreatureState.tint` (which fire damage and corpse decals read) is never changed.
"""

from __future__ import annotations

from collections.abc import Mapping

from grim.color import RGBA

# Creature pool slot -> body colour to draw instead of the creature's own tint (its alpha is kept).
CREATURE_TINT_OVERRIDE: dict[int, RGBA] = {}


def set_creature_tints(mapping: Mapping[int, RGBA]) -> None:
    CREATURE_TINT_OVERRIDE.clear()
    CREATURE_TINT_OVERRIDE.update(mapping)


def clear_creature_tints() -> None:
    CREATURE_TINT_OVERRIDE.clear()


def creature_body_tint(index: int, own: RGBA) -> RGBA:
    """The tint to draw a creature's body with: the override's colour with the creature's own alpha, or its own."""

    override = CREATURE_TINT_OVERRIDE.get(int(index))
    if override is None:
        return own
    return RGBA(override.r, override.g, override.b, own.a)
