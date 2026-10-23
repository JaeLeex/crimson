"""Run a minds session headlessly: the pilot plays, the minds steer, the ledger meters, the replay records."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import msgspec

from ..game_modes import GameMode
from ..game_version import MINDS_RULES
from ..math_parity import f32
from ..replay.recorder import ReplayRecorder
from ..replay.ticks import LiveTickSource, step_replay_tick
from ..replay.types import Replay
from ..sim.commands import CreatureSteerCommand, PerkMenuOpenCommand, PerkPickCommand
from ..sim.run_init import initialize_run
from ..sim.run_result import RunResult, build_run_result
from ..sim.run_spec import RunSpec
from ..sim.state_types import TERRAIN_SIZE
from .ledger import Account, Ledger, Receipt
from .llm import Exchange, LlmMind
from .minds import Mind
from .observe import creature_is_live, observe
from .pilot import AimBot


@dataclass(frozen=True)
class BoundMind:
    """A mind with its account terms."""

    mind: Mind
    budget: int | None = 1000
    price_per_1k_tokens: int = 10

    def account(self, mind_id: int) -> Account:
        return Account(
            mind_id=int(mind_id),
            name=self.mind.name,
            model=self.mind.model,
            budget=None if self.budget is None else int(self.budget),
            balance=0 if self.budget is None else int(self.budget),
            price_per_1k_tokens=int(self.price_per_1k_tokens),
        )


@dataclass(frozen=True)
class MindsRunConfig:
    seed: int = 0xBEEF
    ticks: int = 3600
    # The minds are asked every `cadence` ticks; each steer holds for `hold` ticks (default: the cadence).
    cadence: int = 15
    hold: int | None = None
    # Governed: budgets and ownership enforced. Ungoverned: metered, nothing refused.
    governed: bool = True
    others_limit: int = 12
    game_mode: GameMode = GameMode.SURVIVAL
    perk_choice: int = 0


@dataclass
class MindsRun:
    replay: Replay
    result: RunResult
    accounts: list[Account]
    receipts: list[Receipt]
    transcripts: dict[int, list[Exchange]] = field(default_factory=dict)
    ticks: int = 0
    end_tick: int | None = None
    wall_s: float = 0.0
    mind_s: float = 0.0
    steers_recorded: int = 0

    def summary(self) -> dict[str, object]:
        return {
            "seed": self.replay.run.seed,
            "rules": self.replay.rules,
            "ticks": self.ticks,
            "end_tick": self.end_tick,
            "outcome": str(self.result.outcome),
            "elapsed_ms": self.result.elapsed_ms,
            "kills": self.result.kills,
            "score_xp": self.result.players[0].experience,
            "steers_recorded": self.steers_recorded,
            "wall_s": round(self.wall_s, 3),
            "mind_s": round(self.mind_s, 3),
            "accounts": [msgspec.structs.asdict(account) for account in self.accounts],
        }


def owner_of(creature_index: int, mind_count: int) -> int:
    """Which mind owns a pool slot: slots are dealt round-robin, so every mind gets a share of each wave."""

    return int(creature_index) % int(mind_count)


def run_minds(
    minds: Sequence[BoundMind],
    config: MindsRunConfig | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> MindsRun:
    """Play `config.ticks` ticks (or until the run ends) with `minds` steering the horde; the replay is complete.

    Each mind owns the slots `owner_of` deals it. When asked, it sees those creatures and the nearest others,
    its decision is charged on its tokens, and each steer is authorized by the ledger before it is recorded as a
    `creature_steer` command holding for `config.hold` ticks. The perk menu is opened and the first choice taken
    whenever a perk is pending, so the pilot levels like a player would.
    """

    if config is None:
        config = MindsRunConfig()
    if not minds:
        raise ValueError("a minds run needs at least one mind")
    if config.cadence < 1:
        raise ValueError("cadence must be at least 1")
    hold = config.cadence if config.hold is None else int(config.hold)
    if hold < 1:
        raise ValueError("hold must be at least 1")

    run_spec = RunSpec(game_mode_id=config.game_mode, seed=int(config.seed))
    prepared = initialize_run(run_spec, rules=MINDS_RULES)
    session = prepared.session
    world = session.world
    recorder = ReplayRecorder(run_spec, rules=MINDS_RULES)
    source = LiveTickSource()
    pilot = AimBot()
    ledger = Ledger((bound.account(mind_id) for mind_id, bound in enumerate(minds)), governed=config.governed)
    mind_count = len(minds)

    started = time.perf_counter()
    mind_s = 0.0
    steers_recorded = 0
    end_tick: int | None = None
    tick = 0
    entries = world.creatures.entries

    def live(index: int) -> bool:
        return 0 <= index < len(entries) and creature_is_live(entries[index])

    for tick in range(int(config.ticks)):
        source.poll([pilot.input(world)])
        player = world.players[0]
        if session.perk_menu_open:
            source.submit(PerkPickCommand(player_index=0, choice_index=int(config.perk_choice)))
        elif world.state.perk_selection.pending_count > 0 and player.health > 0.0:
            source.submit(PerkMenuOpenCommand(player_index=0))

        if tick % config.cadence == 0:
            live_indices = [index for index, creature in enumerate(entries) if creature_is_live(creature)]
            for mind_id, bound in enumerate(minds):
                owned = [index for index in live_indices if owner_of(index, mind_count) == mind_id]
                if not owned or not ledger.may_infer(mind_id, tick):
                    continue
                observation = observe(
                    world, tick=tick, elapsed_ms=session.elapsed_ms, owned=owned, others_limit=config.others_limit,
                )
                asked = time.perf_counter()
                decision = bound.mind.decide(observation)
                mind_s += time.perf_counter() - asked
                ledger.charge_inference(
                    mind_id,
                    tick,
                    prompt_tokens=decision.prompt_tokens,
                    completion_tokens=decision.completion_tokens,
                    ok=decision.ok,
                )
                seen: set[int] = set()
                for steer in decision.steers:
                    if steer.creature_index in seen:
                        continue
                    seen.add(steer.creature_index)
                    refused = ledger.authorize_steer(
                        mind_id,
                        tick,
                        steer,
                        owned=lambda index, mind_id=mind_id: owner_of(index, mind_count) == mind_id,
                        live=live,
                        arena=float(TERRAIN_SIZE),
                    )
                    if refused is not None:
                        continue
                    source.submit(
                        CreatureSteerCommand(
                            creature_index=int(steer.creature_index),
                            mind_id=int(mind_id),
                            target_x=f32(steer.target_x),
                            target_y=f32(steer.target_y),
                            hold_ticks=hold,
                        ),
                    )
                    steers_recorded += 1

        replay_tick = source.next_tick()
        recorder.record(replay_tick)
        stepped = step_replay_tick(session, replay_tick)
        if progress is not None:
            progress(tick + 1, int(config.ticks))
        if stepped.outcome is not None:
            end_tick = tick
            break

    outcome = session.end_outcome() if end_tick is not None else session.terminal_outcome() or session.end_outcome()
    result = build_run_result(session, outcome=outcome)
    replay = recorder.finish(result)
    transcripts = {mind_id: list(bound.mind.transcript) for mind_id, bound in enumerate(minds) if isinstance(bound.mind, LlmMind)}
    return MindsRun(
        replay=replay,
        result=result,
        accounts=list(ledger.accounts.values()),
        receipts=ledger.receipts,
        transcripts=transcripts,
        ticks=len(replay.ticks),
        end_tick=end_tick,
        wall_s=time.perf_counter() - started,
        mind_s=mind_s,
        steers_recorded=steers_recorded,
    )
