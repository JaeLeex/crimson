"""Minds: external controllers that steer the creatures, metered and recorded.

A mind (`Mind`) looks at the world (`observe`) every few ticks and proposes targets for the creatures it
owns. The driver (`run_minds`) charges each inference to the mind's credit account (`Ledger`), refuses
steers that are over budget or out of scope, and records every accepted steer as a `creature_steer`
command in the replay. The live run is stochastic (a model's temperature, its server); the recording
replays bit for bit under the minds rules profile (`MINDS_RULES`), so `crimson replay verify` and
`crimson replay render` work on it unchanged. See docs/rewrite/minds.md.
"""

from __future__ import annotations

from .driver import BoundMind, MindsRun, MindsRunConfig, run_minds
from .ledger import Account, Ledger, Receipt
from .llm import Exchange, LlmMind
from .minds import Decision, FlankMind, IdleMind, Mind, RandomMind, Steer
from .observe import CreatureView, Observation, observe

__all__ = [
    "Account",
    "BoundMind",
    "CreatureView",
    "Decision",
    "Exchange",
    "FlankMind",
    "IdleMind",
    "Ledger",
    "LlmMind",
    "Mind",
    "MindsRun",
    "MindsRunConfig",
    "Observation",
    "RandomMind",
    "Receipt",
    "Steer",
    "observe",
    "run_minds",
]
