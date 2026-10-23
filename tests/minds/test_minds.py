from __future__ import annotations

import pickle
from collections.abc import Sequence

import msgspec
import pytest

from crimson.game_modes import GameMode
from crimson.game_version import MINDS_RULES, REPLAY_RULES
from crimson.math_parity import f32
from crimson.minds import BoundMind, Decision, FlankMind, IdleMind, MindsRunConfig, RandomMind, Steer, run_minds
from crimson.minds.llm import parse_steers
from crimson.minds.observe import Observation, creature_is_live, observe
from crimson.replay import ReplayCodecError, dump_replay, load_replay
from crimson.replay.codec import encode_replay_payload
from crimson.replay.driver.playback_driver import PlaybackDriver
from crimson.replay.input_codec import pack_tick
from crimson.replay.ranked import replay_unranked_reasons
from crimson.replay.ticks import step_replay_tick
from crimson.replay.types import ReplayTick
from crimson.sim.commands import CreatureSteerCommand, GameCommand
from crimson.sim.run_init import initialize_run
from crimson.sim.run_result import run_result_mismatches
from crimson.sim.run_spec import RunSpec
from crimson.sim.sessions import DeterministicSession, IllegalCommandError
from crimson.sim.state_types import TERRAIN_SIZE
from grim.geom import Vec2
from tests.support.factories import player_input
from tests.support.state_digest import session_digest

SEED = 0xBEEF
# Survival has spawned its first waves by then.
WARMUP_TICKS = 240


def _survival_session(rules: int) -> DeterministicSession:
    return initialize_run(RunSpec(game_mode_id=GameMode.SURVIVAL, seed=SEED), rules=rules).session


def _idle_tick(commands: Sequence[GameCommand] = ()) -> ReplayTick:
    return pack_tick([player_input(aim=Vec2(512.0, 512.0))], list(commands))


def _steer(index: int, *, x: float = 512.0, y: float = 512.0, hold: int = 1) -> CreatureSteerCommand:
    return CreatureSteerCommand(creature_index=index, mind_id=0, target_x=f32(x), target_y=f32(y), hold_ticks=hold)


def _live_indices(session: DeterministicSession) -> list[int]:
    return [index for index, creature in enumerate(session.world.creatures.entries) if creature_is_live(creature)]


def _verified(run_replay) -> list[str]:
    replay = load_replay(dump_replay(run_replay))
    return run_result_mismatches(replay.result, PlaybackDriver(replay, warn_on_version_mismatch=False).run())


def _steer_commands(ticks: Sequence[ReplayTick]) -> list[CreatureSteerCommand]:
    return [command for tick in ticks for command in tick.commands if isinstance(command, CreatureSteerCommand)]


def test_steering_every_creature_to_its_native_target_changes_nothing() -> None:
    """The steer hook applied with the native AI's own targets leaves the whole session state identical, RNG included."""

    session = _survival_session(MINDS_RULES)
    for _ in range(WARMUP_TICKS):
        step_replay_tick(session, _idle_tick())
    live = _live_indices(session)
    assert live
    snapshot = pickle.dumps(session)

    step_replay_tick(session, _idle_tick())
    entries = session.world.creatures.entries
    targets = {
        index: entries[index].target
        for index in live
        if creature_is_live(entries[index])
        and 0.0 <= entries[index].target.x <= TERRAIN_SIZE
        and 0.0 <= entries[index].target.y <= TERRAIN_SIZE
    }
    assert targets
    plain = session_digest(session)

    session = pickle.loads(snapshot)
    steers = [_steer(index, x=target.x, y=target.y) for index, target in targets.items()]
    step_replay_tick(session, _idle_tick(steers))
    assert session_digest(session) == plain


def test_a_steer_moves_the_creature_toward_the_steered_point() -> None:
    session = _survival_session(MINDS_RULES)
    for _ in range(WARMUP_TICKS):
        step_replay_tick(session, _idle_tick())
    index = _live_indices(session)[0]
    creature = session.world.creatures.entries[index]
    snapshot = pickle.dumps(session)

    corner = Vec2(8.0, 8.0)
    before = (creature.pos - corner).length()
    for _ in range(30):
        step_replay_tick(session, _idle_tick([_steer(index, x=corner.x, y=corner.y)]))
    steered = (session.world.creatures.entries[index].pos - corner).length()

    session = pickle.loads(snapshot)
    for _ in range(30):
        step_replay_tick(session, _idle_tick())
    native = (session.world.creatures.entries[index].pos - corner).length()

    assert steered < before
    assert steered < native


def test_a_steer_holds_for_its_ticks_then_lapses() -> None:
    session = _survival_session(MINDS_RULES)
    for _ in range(WARMUP_TICKS):
        step_replay_tick(session, _idle_tick())
    index = _live_indices(session)[0]

    step_replay_tick(session, _idle_tick([_steer(index, hold=5)]))
    assert [(hold.command.creature_index, hold.ticks_left) for hold in session.steer_holds] == [(index, 4)]
    for ticks_left in (3, 2, 1, 0):
        step_replay_tick(session, _idle_tick())
        assert [hold.ticks_left for hold in session.steer_holds] == ([ticks_left] if ticks_left else [])


def test_illegal_steers_are_refused_by_the_session() -> None:
    session = _survival_session(REPLAY_RULES)
    with pytest.raises(IllegalCommandError, match=f"needs rules {MINDS_RULES}"):
        step_replay_tick(session, _idle_tick([_steer(0)]))

    session = _survival_session(MINDS_RULES)
    for _ in range(WARMUP_TICKS):
        step_replay_tick(session, _idle_tick())
    live = _live_indices(session)
    dead = next(index for index in range(len(session.world.creatures.entries)) if index not in live)
    with pytest.raises(IllegalCommandError, match="no live creature"):
        step_replay_tick(session, _idle_tick([_steer(dead)]))
    with pytest.raises(IllegalCommandError, match="canonical f32 in 0..1024"):
        step_replay_tick(session, _idle_tick([_steer(live[0], x=2048.0)]))
    with pytest.raises(IllegalCommandError, match="twice in one tick"):
        step_replay_tick(session, _idle_tick([_steer(live[0]), _steer(live[0])]))
    with pytest.raises(IllegalCommandError, match="hold_ticks"):
        step_replay_tick(session, _idle_tick([_steer(live[0], hold=0)]))


def test_random_minds_record_a_replay_that_verifies_and_never_ranks() -> None:
    minds = [BoundMind(RandomMind(1), budget=None), BoundMind(RandomMind(2), budget=None)]
    run = run_minds(minds, MindsRunConfig(seed=SEED, ticks=600, governed=False))

    assert run.steers_recorded > 0
    replay = load_replay(dump_replay(run.replay))
    assert replay.rules == MINDS_RULES
    assert len(_steer_commands(replay.ticks)) == run.steers_recorded
    assert {command.mind_id for command in _steer_commands(replay.ticks)} == {0, 1}
    assert _verified(run.replay) == []
    assert "minds" in replay_unranked_reasons(replay)

    other = run_minds([BoundMind(RandomMind(3), budget=None), BoundMind(RandomMind(4), budget=None)], MindsRunConfig(seed=SEED, ticks=600, governed=False))
    assert other.replay.ticks != run.replay.ticks


def test_the_codec_refuses_steers_outside_the_minds_rules() -> None:
    run = run_minds([BoundMind(RandomMind(1), budget=None)], MindsRunConfig(seed=SEED, ticks=300, governed=False))
    assert run.steers_recorded > 0
    with pytest.raises(ReplayCodecError, match=f"require rules {MINDS_RULES}"):
        encode_replay_payload(msgspec.structs.replace(run.replay, rules=REPLAY_RULES))


def test_a_broke_mind_is_refused_and_its_creatures_fall_back_to_the_native_ai() -> None:
    run = run_minds([BoundMind(FlankMind(1), budget=100, price_per_1k_tokens=50)], MindsRunConfig(seed=SEED, ticks=900, governed=True))
    account = run.accounts[0]
    assert account.balance <= 0
    assert account.refusals > 0
    exhausted_at = min(receipt.tick for receipt in run.receipts if receipt.kind == "refusal" and receipt.detail == "budget_exhausted")
    assert max(receipt.tick for receipt in run.receipts if receipt.kind == "steer") < exhausted_at
    assert _steer_commands(run.replay.ticks[exhausted_at + 1 :]) == []
    assert any(receipt.kind == "steer" for receipt in run.receipts)
    assert _verified(run.replay) == []


class _GreedyMind:
    """Steers every creature it is shown, owned or not."""

    name = "greedy"
    model = "test"

    def decide(self, observation: Observation) -> Decision:
        steers = tuple(Steer(creature_index=creature.index, target_x=512.0, target_y=512.0) for creature in (*observation.owned, *observation.others))
        return Decision(steers=steers, prompt_tokens=10, completion_tokens=10)


def test_a_steer_for_a_creature_the_mind_does_not_own_is_refused_when_governed() -> None:
    minds = [BoundMind(_GreedyMind(), budget=None), BoundMind(IdleMind(), budget=None)]
    governed = run_minds(minds, MindsRunConfig(seed=SEED, ticks=600, governed=True))
    greedy = governed.accounts[0]
    assert greedy.steers > 0
    assert greedy.refusals > 0
    assert all(command.mind_id == 0 and command.creature_index % 2 == 0 for command in _steer_commands(governed.replay.ticks))

    ungoverned = run_minds(minds, MindsRunConfig(seed=SEED, ticks=600, governed=False))
    assert ungoverned.accounts[0].refusals == 0
    assert any(command.creature_index % 2 == 1 for command in _steer_commands(ungoverned.replay.ticks))


def test_observation_lists_owned_and_nearest_others() -> None:
    session = _survival_session(MINDS_RULES)
    for _ in range(WARMUP_TICKS):
        step_replay_tick(session, _idle_tick())
    live = _live_indices(session)
    owned = live[::2]
    observation = observe(session.world, tick=WARMUP_TICKS, elapsed_ms=session.elapsed_ms, owned=owned, others_limit=3)
    assert [creature.index for creature in observation.owned] == owned
    assert len(observation.others) == min(3, len(live) - len(owned))
    assert all(creature.index not in owned for creature in observation.others)
    player = session.world.players[0]
    distances = [(Vec2(creature.x, creature.y) - player.pos).length() for creature in observation.others]
    assert distances == sorted(distances)


def test_model_replies_are_parsed_leniently_and_clamped() -> None:
    observation = observe(_survival_session(MINDS_RULES).world, tick=0, elapsed_ms=0.0, owned=())
    observation = msgspec.structs.replace(
        observation,
        owned=(msgspec.structs.replace(observation.owned[0], index=7) if observation.owned else _view(7),),
    )
    reply = 'Sure! {"steers":[{"i":7,"x":-50,"y":5000},{"i":9,"x":1,"y":1},{"i":"7","x":"bad"}]} done'
    assert parse_steers(reply, observation) == (Steer(creature_index=7, target_x=0.0, target_y=1024.0),)
    assert parse_steers("no json here", observation) == ()
    assert parse_steers('{"steers": "nope"}', observation) == ()


def _view(index: int):
    from crimson.minds.observe import CreatureView

    return CreatureView(index=index, type_id=0, x=100.0, y=100.0, hp=10.0, max_hp=10.0, heading=0.0, ai_mode=0, size=50.0)


def test_the_arena_quest_compiles_and_ends_the_run_under_native_ai() -> None:
    from crimson.minds.arena import arena_quest
    from crimson.sim.run_result import RunOutcome

    quest = arena_quest()
    assert sum(entry.count for entry in quest.entries) >= 90
    assert sorted({entry.trigger_ms for entry in quest.entries}) == [0, 14000, 28000, 42000]
    run = run_minds([BoundMind(IdleMind(), budget=None)], MindsRunConfig(seed=SEED, ticks=7200, governed=False, scene="arena"))
    assert run.result.outcome in (RunOutcome.DEATH, RunOutcome.QUEST_COMPLETED)
    assert run.end_tick is not None
    assert _verified(run.replay) == []


def test_moves_resolve_to_points_around_the_player() -> None:
    from crimson.minds.tactics import MOVES, move_target

    observation = msgspec.structs.replace(
        observe(_survival_session(MINDS_RULES).world, tick=0, elapsed_ms=0.0, owned=()),
        player_x=512.0,
        player_y=512.0,
        player_aim_x=612.0,
        player_aim_y=512.0,
    )
    far = _view(1)  # at (100, 100), far from the player
    targets = {move: move_target(move, observation, far) for move in MOVES}
    assert targets["rush"] == (512.0, 512.0)
    assert targets["hold"] == (100.0, 100.0)
    # Aim points east; left on screen (y down) is north, right is south, behind is west.
    assert targets["flank_left"] == pytest.approx((512.0, 362.0))
    assert targets["flank_right"] == pytest.approx((512.0, 662.0))
    assert targets["behind"] == pytest.approx((362.0, 512.0))
    with pytest.raises(ValueError, match="unknown move"):
        move_target("teleport", observation, far)


def test_model_move_replies_are_parsed_and_unknown_moves_dropped() -> None:
    from crimson.minds.llm import parse_moves

    observation = msgspec.structs.replace(
        observe(_survival_session(MINDS_RULES).world, tick=0, elapsed_ms=0.0, owned=()),
        player_x=512.0,
        player_y=512.0,
        player_aim_x=612.0,
        player_aim_y=512.0,
        owned=(_view(7),),
    )
    reply = '{"moves":[{"i":7,"m":"Flank_Left"},{"i":8,"m":"rush"},{"i":7,"m":"teleport"}]}'
    steers = parse_moves(reply, observation)
    assert [steer.creature_index for steer in steers] == [7]
    assert (steers[0].target_x, steers[0].target_y) == (512.0, 362.0)
    assert parse_moves('{"moves":[{"i":7,"m":"nope"}]}', observation) == ()
    # A default move covers every owned creature the exceptions leave out.
    observation = msgspec.structs.replace(observation, owned=(_view(7), _view(8)))
    steers = parse_moves('{"default":"rush","moves":[{"i":8,"m":"hold"}]}', observation)
    assert [(steer.creature_index, steer.target_x, steer.target_y) for steer in steers] == [(7, 512.0, 512.0), (8, 100.0, 100.0)]


def test_the_pilot_skill_dial_changes_the_damage_the_horde_lands() -> None:
    from crimson.minds.pilot import PilotSkill

    shooting = run_minds([BoundMind(IdleMind(), budget=None)], MindsRunConfig(seed=SEED, ticks=1800, governed=False, scene="arena", pilot=PilotSkill()))
    unarmed = run_minds(
        [BoundMind(IdleMind(), budget=None)],
        MindsRunConfig(seed=SEED, ticks=1800, governed=False, scene="arena", pilot=PilotSkill(fire_on=0, fire_off=1)),
    )
    assert unarmed.damage_taken >= shooting.damage_taken
    assert unarmed.result.kills <= shooting.result.kills
