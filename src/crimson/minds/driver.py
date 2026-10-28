"""Run a minds session headlessly: the pilot plays, the minds steer, the ledger meters, the replay records."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from ..game_modes import GameMode
from ..game_version import MINDS_RULES
from ..math_parity import f32
from ..quests.custom import CustomQuest
from ..replay.codec import encode_replay_payload
from ..replay.recorder import ReplayRecorder
from ..replay.ticks import LiveTickSource, step_replay_tick
from ..replay.types import Replay
from ..sim.commands import CreatureSteerCommand, PerkMenuOpenCommand, PerkPickCommand
from ..sim.run_init import initialize_run
from ..sim.run_result import RunResult, build_run_result
from ..sim.run_spec import RunSpec
from ..sim.state_types import TERRAIN_SIZE
from .jev import JevExchange, JevMind
from .ledger import Account, Ledger, Receipt
from .llm import Exchange, LlmMind
from .minds import Mind
from .nunchi import settlement_receipt, sha256_hex, write_lease
from .observe import Observation, creature_is_live, observe
from .pilot import AimBot, PilotSkill


@dataclass(frozen=True)
class BoundMind:
    """A mind with its account terms."""

    mind: Mind
    budget: int | None = 1000
    price_per_1k_tokens: int = 10
    # What the model's tokens cost in US dollars per million (Jev publishes $0.042); settles as micro-USD on chain.
    usd_per_mtok: float = 0.0

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
    # "arena" / "edge" / "ring" play the dense custom quests (`crimson.minds.arena`); "survival" the stock trickle.
    scene: str = "arena"
    pilot: PilotSkill = field(default_factory=PilotSkill)
    perk_choice: int = 0
    # Ask the minds concurrently (a model server with several slots answers them together).
    parallel: bool = True
    # Nunchi settlement (`crimson.minds.nunchi.NunchiChain`): admit each mind on chain before play, settle after.
    nunchi: Any = None
    # Where the leases are written (the run's output directory); required with `nunchi`.
    settle_dir: Path | None = None
    # A unique run id: the minds' on-chain principals are crimson/<run_id>/mind-<n>.
    run_id: str = ""
    # Credits issued on chain to a mind with no local budget (an ungoverned mind still settles against the chain).
    chain_budget: int = 2000
    # Read each mind's on-chain permission every this many ticks; a revoked mind is refused (governed runs).
    permission_check_every: int = 300
    # Demo of the operator's stop switch: revoke `revoke_mind` on chain at this tick.
    revoke_at_tick: int | None = None
    revoke_mind: int = 0
    # A custom quest to play instead of `scene` (for example one a builder wrote: `crimson.minds.build`).
    quest: CustomQuest | None = None

    def run_spec(self) -> RunSpec:
        if self.quest is not None:
            return RunSpec(game_mode_id=GameMode.QUESTS, seed=int(self.seed), custom_quest=self.quest)
        match self.scene:
            case "arena" | "edge" | "ring":
                from .arena import arena_quest

                preset = "edge" if self.scene == "arena" else self.scene
                return RunSpec(game_mode_id=GameMode.QUESTS, seed=int(self.seed), custom_quest=arena_quest(preset))
            case "survival":
                return RunSpec(game_mode_id=GameMode.SURVIVAL, seed=int(self.seed))
            case _:
                raise ValueError(f"unknown scene {self.scene!r}; use arena (edge), ring or survival")


@dataclass
class MindsRun:
    replay: Replay
    result: RunResult
    accounts: list[Account]
    receipts: list[Receipt]
    transcripts: dict[int, list[Exchange | JevExchange]] = field(default_factory=dict)
    ticks: int = 0
    end_tick: int | None = None
    wall_s: float = 0.0
    mind_s: float = 0.0
    steers_recorded: int = 0
    # The receipt log exactly as written beside the replay (its SHA-256 is part of the settlement receipt).
    receipts_jsonl: bytes = b""
    # On-chain admission and settlement per mind, when the run was settled on Nunchi.
    settlement: dict[str, Any] | None = None

    @property
    def player_health(self) -> float:
        return float(self.result.players[0].health)

    @property
    def damage_taken(self) -> float:
        """Health lost by the end, floored at a full bar's worth: the horde's score against the pilot."""

        return max(0.0, min(100.0, 100.0 - self.player_health))

    def summary(self) -> dict[str, object]:
        return {
            "seed": self.replay.run.seed,
            "rules": self.replay.rules,
            "scene": "quest" if self.replay.run.custom_quest is not None else "survival",
            "ticks": self.ticks,
            "end_tick": self.end_tick,
            "outcome": str(self.result.outcome),
            "elapsed_ms": self.result.elapsed_ms,
            "player_health": self.player_health,
            "damage_taken": self.damage_taken,
            "kills": self.result.kills,
            "score_xp": self.result.players[0].experience,
            "steers_recorded": self.steers_recorded,
            "wall_s": round(self.wall_s, 3),
            "mind_s": round(self.mind_s, 3),
            "accounts": [msgspec.structs.asdict(account) for account in self.accounts],
            "settlement": self.settlement,
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

    run_spec = config.run_spec()
    prepared = initialize_run(run_spec, rules=MINDS_RULES)
    session = prepared.session
    world = session.world
    recorder = ReplayRecorder(run_spec, rules=MINDS_RULES)
    source = LiveTickSource()
    pilot = AimBot(config.pilot)
    ledger = Ledger((bound.account(mind_id) for mind_id, bound in enumerate(minds)), governed=config.governed)
    mind_count = len(minds)
    run_id = config.run_id or uuid.uuid4().hex[:12]
    principals = {mind_id: f"crimson/{run_id}/mind-{mind_id}" for mind_id in range(mind_count)}
    admissions: dict[int, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    if config.nunchi is not None:
        admissions = _admit(minds, config, ledger, principals, run_id)

    started = time.perf_counter()
    mind_s = 0.0
    steers_recorded = 0
    end_tick: int | None = None
    tick = 0
    entries = world.creatures.entries

    def live(index: int) -> bool:
        return 0 <= index < len(entries) and creature_is_live(entries[index])

    for tick in range(int(config.ticks)):
        if config.nunchi is not None:
            _chain_checks(config, ledger, minds, principals, tick, events)
        source.poll([pilot.input(world)])
        player = world.players[0]
        if session.perk_menu_open:
            source.submit(PerkPickCommand(player_index=0, choice_index=int(config.perk_choice)))
        elif world.state.perk_selection.pending_count > 0 and player.health > 0.0:
            source.submit(PerkMenuOpenCommand(player_index=0))

        if tick % config.cadence == 0:
            live_indices = [index for index, creature in enumerate(entries) if creature_is_live(creature)]
            asks: list[tuple[int, BoundMind, Observation]] = []
            for mind_id, bound in enumerate(minds):
                owned = [index for index in live_indices if owner_of(index, mind_count) == mind_id]
                if not owned or not ledger.may_infer(mind_id, tick):
                    continue
                observation = observe(
                    world, tick=tick, elapsed_ms=session.elapsed_ms, owned=owned, others_limit=config.others_limit,
                )
                asks.append((mind_id, bound, observation))
            asked = time.perf_counter()
            if config.parallel and len(asks) > 1:
                with ThreadPoolExecutor(max_workers=len(asks)) as pool:
                    decisions = list(pool.map(lambda ask: ask[1].mind.decide(ask[2]), asks))
            else:
                decisions = [bound.mind.decide(observation) for _, bound, observation in asks]
            mind_s += time.perf_counter() - asked
            # The ledger and the recording follow mind order whatever order the answers arrived in.
            for (mind_id, _bound, _observation), decision in zip(asks, decisions, strict=True):
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
    receipts_jsonl = ledger.receipts_jsonl()
    settlement = None
    if config.nunchi is not None:
        settlement = _settle(minds, config, ledger, principals, admissions, events, replay, receipts_jsonl, run_id)
    transcripts: dict[int, list[Exchange | JevExchange]] = {
        mind_id: list(bound.mind.transcript) for mind_id, bound in enumerate(minds) if isinstance(bound.mind, (LlmMind, JevMind))
    }
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
        receipts_jsonl=receipts_jsonl,
        settlement=settlement,
    )


def _admit(
    minds: Sequence[BoundMind],
    config: MindsRunConfig,
    ledger: Ledger,
    principals: dict[int, str],
    run_id: str,
) -> dict[int, dict[str, Any]]:
    """Admit every mind on chain (one at a time) and make the local accounts hold what the chain issued."""

    settle_dir = config.settle_dir
    if settle_dir is None:
        raise ValueError("a Nunchi-settled run needs settle_dir for its leases")
    chain = config.nunchi

    def admit_one(mind_id: int) -> tuple[int, dict[str, Any]]:
        bound = minds[mind_id]
        budget = int(bound.budget) if bound.budget is not None else int(config.chain_budget)
        lease = {
            "kind": "crimson-minds/lease/v1",
            "run_id": run_id,
            "principal": principals[mind_id],
            "mind": bound.mind.name,
            "model": bound.mind.model,
            "budget_credits": budget,
            "price_per_1k_tokens": int(bound.price_per_1k_tokens),
            "usd_per_mtok": float(bound.usd_per_mtok),
            "governed": bool(config.governed),
            "run_seed": int(config.seed),
            "scene": config.scene,
            "cadence": int(config.cadence),
        }
        lease_path = Path(settle_dir) / f"lease-mind-{mind_id}.json"
        lease_bytes = write_lease(lease_path, lease)
        admitted = chain.admit(principal=principals[mind_id], model=bound.mind.model, budget=budget, lease=lease_path)
        admitted["lease_file"] = lease_path.name
        admitted["lease_sha256"] = sha256_hex(lease_bytes)
        return mind_id, admitted

    # One writer account signs every settlement write, so writes go one at a time: two in flight would take the
    # same nonce and the chain would keep only one of them.
    admissions = dict(admit_one(mind_id) for mind_id in range(len(minds)))
    for mind_id, admitted in admissions.items():
        account = ledger.accounts[mind_id]
        if minds[mind_id].budget is not None:
            # The chain is the source of the budget the game enforces.
            account.budget = int(admitted["balance"])
            account.balance = int(admitted["balance"])
    return admissions


def _chain_checks(
    config: MindsRunConfig,
    ledger: Ledger,
    minds: Sequence[BoundMind],
    principals: dict[int, str],
    tick: int,
    events: list[dict[str, Any]],
) -> None:
    """The operator's stop switch (demo) and the periodic read of each mind's on-chain permission."""

    chain = config.nunchi
    if config.revoke_at_tick is not None and tick == int(config.revoke_at_tick):
        mind_id = int(config.revoke_mind)
        revoked = chain.revoke(principal=principals[mind_id], model=minds[mind_id].mind.model)
        events.append({"tick": tick, "event": "operator revoked on chain", "mind": mind_id, "tx": revoked["txs"][0]})
        ledger.revoke(mind_id, tick)
        return
    if tick == 0 or tick % max(1, int(config.permission_check_every)) != 0:
        return
    for mind_id, bound in enumerate(minds):
        if mind_id in ledger.revoked:
            continue
        if not chain.permitted(principal=principals[mind_id], model=bound.mind.model):
            events.append({"tick": tick, "event": "chain permission found revoked", "mind": mind_id})
            ledger.revoke(mind_id, tick)


def _settle(
    minds: Sequence[BoundMind],
    config: MindsRunConfig,
    ledger: Ledger,
    principals: dict[int, str],
    admissions: dict[int, dict[str, Any]],
    events: list[dict[str, Any]],
    replay: Replay,
    receipts_jsonl: bytes,
    run_id: str,
) -> dict[str, Any]:
    """Settle every mind on chain (one at a time): debit what it spent, record usage bound to the recording."""

    chain = config.nunchi
    replay_sha = sha256_hex(encode_replay_payload(replay))
    receipts_sha = sha256_hex(receipts_jsonl)

    def settle_one(mind_id: int) -> tuple[int, dict[str, Any]]:
        account = ledger.accounts[mind_id]
        receipt = settlement_receipt(replay_sha256=replay_sha, receipts_sha256=receipts_sha, principal=principals[mind_id])
        cost_usd_micros = int(math.ceil(account.tokens * float(minds[mind_id].usd_per_mtok)))
        settled = chain.settle(
            principal=principals[mind_id], model=minds[mind_id].mind.model, spent=int(account.spent),
            cost_usd_micros=cost_usd_micros, receipt=receipt,
        )
        return mind_id, settled

    settled = dict(settle_one(mind_id) for mind_id in range(len(minds)))
    return {
        "chain_rpc": chain.rpc,
        "run_id": run_id,
        "replay_sha256": replay_sha,
        "receipts_sha256": receipts_sha,
        "events": events,
        "minds": [
            {"mind": mind_id, "principal": principals[mind_id], "admission": admissions[mind_id], "settlement": settled[mind_id]}
            for mind_id in range(len(minds))
        ],
    }
