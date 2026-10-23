from __future__ import annotations

from collections.abc import Sequence

import msgspec

from grim.sfx_map import SfxId
from grim.sfx_types import SfxRequest

from ..camera import camera_update_for_players
from ..creatures.lifecycle import creature_lifecycle_is_alive
from ..game_modes import GameMode
from ..game_version import MINDS_RULES
from ..math_parity import f32
from ..perks.availability import prepare_perk_availability
from ..perks.selection import (
    PerkPick,
    perk_selection_pick,
)
from ..rng_caller_static import RngCallerStatic
from ..tutorial.runtime import tutorial_input_transform
from ..typo.runtime import TYPO_TIME_SCALE_FACTOR, TypoCommand, typo_gameplay_update
from ..weapon_runtime.availability import prepare_weapon_availability
from .commands import (
    STEER_HOLD_MAX_TICKS,
    CreatureSteerCommand,
    GameCommand,
    PerkMenuOpenCommand,
    PerkPickCommand,
    TypoBackspaceCommand,
    TypoCharCommand,
    TypoSubmitCommand,
)
from .input import PlayerInput
from .mode_updates import ModeState, QuestSpawnState
from .presentation_step import DeterministicPresentationPlan
from .run_result import RunOutcome, all_players_dead, death_transition_ready
from .smoke_events import AtmosphereImpulse, SmokeEmission
from .state_types import TERRAIN_SIZE
from .terrain_fx import TerrainFxScratch
from .timing import FrameTiming, reflex_boost_time_scale_factor
from .world_state import WorldEvents, WorldState

# ---------------------------------------------------------------------------
# Tick result types
# ---------------------------------------------------------------------------


class DeterministicSessionTick(msgspec.Struct):
    dt_sim: float
    timing: FrameTiming
    events: WorldEvents
    presentation: DeterministicPresentationPlan
    elapsed_ms: float = 0.0
    creature_count_world_step: int = 0
    # Native called `game_save_status` during this tick; the live mode writes the status file.
    save_status: bool = False
    # Set on the tick that ends the run; a valid replay ends on this tick.
    outcome: RunOutcome | None = None
    # The perks picked before this tick, as the menu offered them.
    perk_picks: tuple[PerkPick, ...] = ()


class IllegalCommandError(ValueError):
    """A command the live UI could not have issued in the current state."""


# ---------------------------------------------------------------------------
# Mode runtime system
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared timing helper
# ---------------------------------------------------------------------------


def _session_timing(world: WorldState, dt: float) -> FrameTiming:
    """Compute frame timing from world state. Used by all session types."""
    state = world.state
    if state.game_mode == GameMode.TYPO:
        # `game_frame_update` applies Reflex Boosted only in `GAME_STATE_GAMEPLAY`.
        return FrameTiming.compute(
            dt, time_scale_active_entry=bool(state.time_scale_active), time_scale_factor=TYPO_TIME_SCALE_FACTOR,
        )
    world_dt = world.world_dt_after_perk_steps(dt)
    return FrameTiming.compute(
        dt,
        world_dt=world_dt,
        time_scale_active_entry=bool(state.time_scale_active),
        time_scale_factor=reflex_boost_time_scale_factor(
            reflex_boost_timer=float(state.bonuses.reflex_boost),
            time_scale_active=bool(state.time_scale_active),
        ),
    )


# ---------------------------------------------------------------------------
# Unified deterministic session (replaces Survival/Rush/Tutorial/Typo/WorldTick)
# ---------------------------------------------------------------------------


class SteerHold(msgspec.Struct):
    """A `creature_steer` still in force: it steers its slot for `ticks_left` more ticks (minds rules)."""

    command: CreatureSteerCommand
    ticks_left: int


class DeterministicSession(msgspec.Struct):
    # Core state
    world: WorldState

    perk_progression_enabled: bool

    # Mutable timing
    elapsed_ms: float = 0.0
    terrain_fx: TerrainFxScratch = msgspec.field(default_factory=TerrainFxScratch)

    mode_state: ModeState = None
    # The perk menu opened in the last tick: the next tick may start with one pick, and closes it either way.
    perk_menu_open: bool = False
    # Steers still in force from earlier ticks, by slot order (minds rules; see `SteerHold`).
    steer_holds: list[SteerHold] = msgspec.field(default_factory=list)

    @classmethod
    def start(cls, world: WorldState, *, perk_progression_enabled: bool, mode_state: ModeState = None) -> DeterministicSession:
        """A run's session: the weapons and perks it may hand out are fixed as it starts. Constructing one is plain,
        so a keyframe restores a session exactly as it stood."""
        prepare_weapon_availability(world.state)
        prepare_perk_availability(world.state)
        return cls(world=world, perk_progression_enabled=perk_progression_enabled, mode_state=mode_state)

    def timing_for_dt(self, dt: float) -> FrameTiming:
        return _session_timing(self.world, dt)

    @property
    def run_elapsed_ms(self) -> float:
        """Elapsed run time as scored: the spawn timeline for quests, session time otherwise."""

        if isinstance(self.mode_state, QuestSpawnState):
            return float(self.mode_state.spawn_timeline_ms)
        return float(self.elapsed_ms)

    def terminal_outcome(self) -> RunOutcome | None:
        """Outcome when this tick ends the run in live play, else None."""

        players = self.world.players
        match self.world.state.game_mode:
            case GameMode.SURVIVAL | GameMode.RUSH:
                return RunOutcome.DEATH if death_transition_ready(players) else None
            case GameMode.QUESTS:
                # `gameplay_update_and_render` checks for death after `quest_mode_update`, so a
                # death replaces pending quest results.
                if death_transition_ready(players):
                    return RunOutcome.DEATH
                if isinstance(self.mode_state, QuestSpawnState) and self.mode_state.completed:
                    return RunOutcome.QUEST_COMPLETED
                return None
            case GameMode.TYPO:
                # `typo_gameplay_update_and_render` plays the death animation out, like Survival.
                return RunOutcome.DEATH if death_transition_ready(players) else None
            case _:
                return None

    def end_outcome(self) -> RunOutcome:
        """Outcome of a run whose recording stops after the current tick."""

        match self.world.state.game_mode:
            case GameMode.QUESTS:
                # The failed-quest countdown keeps running while paused, so a
                # failed run may close between ticks before the death animation ends.
                outcome = self.terminal_outcome()
                if outcome is not None:
                    return outcome
                return RunOutcome.DEATH if all_players_dead(self.world.players) else RunOutcome.INCOMPLETE
            case GameMode.TUTORIAL:
                # The tutorial has no terminal tick: players leave it from the UI.
                stage_index = int(self.world.state.tutorial.stage_index)
                return RunOutcome.TUTORIAL_COMPLETED if stage_index >= 8 else RunOutcome.INCOMPLETE
            case _:
                return self.terminal_outcome() or RunOutcome.INCOMPLETE

    def _mode_inputs(self, inputs: Sequence[PlayerInput]) -> Sequence[PlayerInput]:
        match self.world.state.game_mode:
            case GameMode.TUTORIAL:
                return tutorial_input_transform(self.world, inputs)
            case _:
                return inputs

    def _require_perk_command_allowed(self, name: str) -> None:
        # The perk prompt only offers the menu while a perk is pending and a
        # player is alive; each command is checked against the state left by
        # the commands before it.
        if int(self.world.state.perk_selection.pending_count) <= 0:
            raise IllegalCommandError(f"{name} without a pending perk")
        if all_players_dead(self.world.players):
            raise IllegalCommandError(f"{name} while every player is dead")

    def _require_steer_allowed(self, command: CreatureSteerCommand, steers: dict[int, CreatureSteerCommand]) -> None:
        # A steer is legal only under the minds rules, for a live creature, once per tick, and onto the arena.
        if self.world.state.rules != MINDS_RULES:
            raise IllegalCommandError(f"creature_steer under rules {self.world.state.rules}; it needs rules {MINDS_RULES}")
        if self.world.state.game_mode == GameMode.TYPO:
            raise IllegalCommandError("creature_steer in Typ-o")
        entries = self.world.creatures.entries
        index = int(command.creature_index)
        if not 0 <= index < len(entries):
            raise IllegalCommandError(f"creature_steer of slot {index} outside 0..{len(entries) - 1}")
        if index in steers:
            raise IllegalCommandError(f"creature_steer of slot {index} twice in one tick")
        creature = entries[index]
        if not creature.active or creature.hp <= 0.0 or not creature_lifecycle_is_alive(creature.death_timer):
            raise IllegalCommandError(f"creature_steer of slot {index}, which holds no live creature")
        for name, value in (("target_x", command.target_x), ("target_y", command.target_y)):
            if not (f32(value) == value and 0.0 <= value <= TERRAIN_SIZE):
                raise IllegalCommandError(f"creature_steer {name} {value!r} must be a canonical f32 in 0..{TERRAIN_SIZE:g}")
        if not 1 <= int(command.hold_ticks) <= STEER_HOLD_MAX_TICKS:
            raise IllegalCommandError(f"creature_steer hold_ticks {command.hold_ticks} must be in 1..{STEER_HOLD_MAX_TICKS}")

    def _steers_in_force(self, fresh: dict[int, CreatureSteerCommand]) -> dict[int, CreatureSteerCommand]:
        """This tick's steers: the fresh commands, then the holds still in force for slots they did not replace.

        A hold ends when its ticks run out, when a fresh steer replaces it, or when its slot no longer holds a
        live creature (a later spawn in the same slot starts unsteered). Fresh steers with more than one tick
        become holds for the ticks after this one.
        """

        entries = self.world.creatures.entries
        steers = dict(fresh)
        holds: list[SteerHold] = []
        for hold in self.steer_holds:
            index = int(hold.command.creature_index)
            if index in fresh:
                continue
            creature = entries[index]
            if not creature.active or creature.hp <= 0.0 or not creature_lifecycle_is_alive(creature.death_timer):
                continue
            steers[index] = hold.command
            if hold.ticks_left > 1:
                holds.append(SteerHold(command=hold.command, ticks_left=hold.ticks_left - 1))
        for command in fresh.values():
            if command.hold_ticks > 1:
                holds.append(SteerHold(command=command, ticks_left=int(command.hold_ticks) - 1))
        holds.sort(key=lambda hold: int(hold.command.creature_index))
        self.steer_holds = holds
        return steers

    def apply_command(self, command: GameCommand, *, dt: float) -> PerkPick:
        match command:
            case PerkPickCommand(choice_index=choice_index):
                self._require_perk_command_allowed("perk_pick")
                # Each pick sees any timing changes made by earlier picks.
                timing = self.timing_for_dt(dt)
                picked = perk_selection_pick(
                    self.world.state,
                    self.world.players,
                    choice_index,
                    game_mode=self.world.state.game_mode,
                    dt=timing.dt_sim,
                    creatures=self.world.creatures.entries,
                )
                if picked is None:
                    raise IllegalCommandError("perk_pick of a choice that is not offered")
                return picked
            case _:
                raise RuntimeError(f"unhandled command type: {type(command).__name__}")

    def step_tick(
        self,
        *,
        dt: float,
        inputs: Sequence[PlayerInput],
        commands: Sequence[GameCommand] | None = None,
    ) -> DeterministicSessionTick:
        # Command effects belong to this tick too. Always detach the sink on
        # failure, and leave no presentation history in simulation keyframes.
        impulses: list[AtmosphereImpulse] = []
        with (
            self.world.state.sprite_effects.capture_emissions(impulses=impulses) as emissions,
            self.world.state.effects.capture_emissions(emissions, impulses=impulses),
        ):
            return self._step_tick(dt=dt, inputs=inputs, commands=commands,
                                   smoke_emissions=emissions, atmosphere_impulses=impulses)

    def _step_tick(
        self,
        *,
        dt: float,
        inputs: Sequence[PlayerInput],
        commands: Sequence[GameCommand] | None,
        smoke_emissions: list[SmokeEmission],
        atmosphere_impulses: list[AtmosphereImpulse],
    ) -> DeterministicSessionTick:
        post_apply_sfx: list[SfxId] = []
        perk_picks: list[PerkPick] = []
        typo_commands: list[TypoCommand] = []
        steers: dict[int, CreatureSteerCommand] = {}
        open_perk_menu = False
        # The perk screen pauses the game: a pick comes right after the menu opened, and a tick without one is Cancel.
        menu_open, self.perk_menu_open = self.perk_menu_open, False
        for command in commands or ():
            if self.world.state.game_mode == GameMode.RUSH and isinstance(command, (PerkPickCommand, PerkMenuOpenCommand)):
                raise IllegalCommandError("perk command in Rush")
            match command:
                case PerkPickCommand():
                    if not menu_open:
                        raise IllegalCommandError("perk_pick without an open perk menu")
                    menu_open = False
                    perk_picks.append(self.apply_command(command, dt=dt))
                    post_apply_sfx.append(SfxId.UI_BONUS)
                case PerkMenuOpenCommand():
                    # Live play requests the menu only while it may open.
                    self._require_perk_command_allowed("perk_menu_open")
                    open_perk_menu = True
                case TypoCharCommand() | TypoBackspaceCommand() | TypoSubmitCommand():
                    if self.world.state.game_mode != GameMode.TYPO:
                        raise IllegalCommandError(f"Typ-o command in non-Typo session: {type(command).__name__}")
                    typo_commands.append(command)
                case CreatureSteerCommand():
                    self._require_steer_allowed(command, steers)
                    steers[int(command.creature_index)] = command
                case _:
                    raise RuntimeError(f"unhandled command type: {type(command).__name__}")

        steers = self._steers_in_force(steers)

        # Picks belong to the between-tick prelude (the perk screen pauses the
        # game). A menu request opens mid-tick at the native point, see
        # `WorldState.step`. Typ-o input belongs inside the tick, at the start of
        # `typo_gameplay_update_and_render`.
        timing = self.timing_for_dt(dt)
        state = self.world.state
        dt_sim_ms = float(timing.dt_sim_ms_i32)
        elapsed_before_ms = self.elapsed_ms

        fx_queue = self.terrain_fx.decals
        fx_queue_rotated = self.terrain_fx.corpses
        if state.game_mode == GameMode.TYPO:
            events = typo_gameplay_update(
                self.world,
                commands=typo_commands,
                timing=timing,
                fx_queue=fx_queue,
                fx_queue_rotated=fx_queue_rotated,
                elapsed_ms=elapsed_before_ms,
            )
        else:
            events = self.world.step(
                timing.dt_sim,
                inputs=self._mode_inputs(inputs),
                fx_queue=fx_queue,
                fx_queue_rotated=fx_queue_rotated,
                perk_progression_enabled=self.perk_progression_enabled,
                mode_state=self.mode_state,
                elapsed_ms=elapsed_before_ms,
                open_perk_menu=open_perk_menu,
                steers=steers or None,
            )
            self.perk_menu_open = events.perk_menu_opened

        quest_spawn = self.mode_state if isinstance(self.mode_state, QuestSpawnState) else None
        if quest_spawn is not None and quest_spawn.play_hit_sfx:
            post_apply_sfx.append(SfxId.QUESTHIT)
        self.elapsed_ms = elapsed_before_ms + dt_sim_ms
        presentation = DeterministicPresentationPlan(
            trigger_game_tune=events.trigger_game_tune,
            sfx=(*events.hit_sfx, *events.sfx),
            terrain_fx=self.terrain_fx.take_batch(),
            post_apply_sfx=tuple(SfxRequest(sfx) for sfx in post_apply_sfx),
            play_quest_completion_music=quest_spawn is not None and quest_spawn.play_completion_music,
            reflex_boost_timer=float(state.bonuses.reflex_boost),
            sfx_dt=timing.dt_audio,
            dt_sim=timing.dt_sim,
            camera=camera_update_for_players(self.world.players, state.camera_shake_offset),
            smoke_emissions=tuple(smoke_emissions),
            atmosphere_impulses=tuple(atmosphere_impulses),
        )
        # `quest_mode_update` saves the status on each frame of its `timer > 2500` branch.
        step = DeterministicSessionTick(
            dt_sim=timing.dt_sim,
            timing=timing,
            events=events,
            presentation=presentation,
            elapsed_ms=self.elapsed_ms,
            creature_count_world_step=events.creature_count_before_render,
            save_status=quest_spawn is not None and quest_spawn.completed,
            outcome=self.terminal_outcome(),
            perk_picks=tuple(perk_picks),
        )
        # `game_frame_update` ends every frame with a discarded draw. Frames outside
        # gameplay (pause, perk menu) draw too in native; they depend on wall-clock
        # time, so the port fixes them at zero.
        state.rng.rand_tagged(RngCallerStatic.GAME_FRAME_UPDATE_DISCARDED)
        return step
