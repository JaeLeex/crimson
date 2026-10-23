---
tags:
  - rewrite
  - contracts
---

# Minds: steered creatures

A **mind** is a controller outside the simulation that sets targets for creatures: a language model, a
heuristic, a random walk, a bot. The creatures keep their native AI step; a mind only replaces the point a
creature turns toward. Every accepted steer is recorded in the replay as a `creature_steer` command, so a
run that was stochastic while it played (a model's temperature, its server) replays exactly afterwards,
and `crimson replay verify`, `play` and `render` work on it unchanged.

Minds exist to study governed autonomy: each mind spends credits on its inferences, is refused when it is
out of budget or steers a creature it does not own, and falls back to the native AI when refused. The
run is the evidence: what each mind was told, what it asked for, what it paid, what was refused, and
what happened.

## Decisions

- **Rules 3, the minds profile.** `MINDS_RULES` plays rules-2 gameplay and admits `creature_steer`
  commands; rules 1 and 2 refuse them. A rules-3 replay never ranks (`replay_unranked_reasons` adds
  `minds`) and the leaderboard's verifier does not play it. Nothing about rules 1 or 2 changes.
- **The steer replaces the target, not the AI.** `CreaturePool.update` runs `creature_ai_update_target`
  as native does, then `creature_apply_steer` overwrites `target` and recomputes `target_heading` the way
  the native tail does (f32 deltas into atan2). `ai_mode` resets, `move_scale`, link-death damage, movement,
  cooldowns and every RNG draw stay native. A steer whose target equals the native one changes nothing in
  the session state (`tests/minds/test_minds.py`).
- **A steer is an input.** It travels in the tick's `commands`, is validated like any command (a live
  creature, once per tick, a canonical f32 target on the 1024×1024 arena, `hold_ticks` 1..600) and is
  refused with `IllegalCommandError` otherwise. Playback needs no new code path.
- **Holds.** A steer holds its slot for `hold_ticks` ticks (this one included), until a later steer of the
  slot replaces it or the slot stops holding a live creature; a later spawn in the same slot starts
  unsteered. Holds live in `DeterministicSession.steer_holds` and are part of the session state.
- **Minds never draw from `state.rng`.** A model-free mind that needs randomness carries its own
  `random.Random`; a model's sampling is the model server's. The replay records the outcome, not the draw.
- **Charges follow reported usage.** A language model is charged on the server's `prompt_tokens` plus
  `completion_tokens` at the mind's price per thousand tokens, never on a declared number; a failed call
  costs nothing and is counted. The model-free minds report a stand-in count (a fixed prompt overhead
  plus a per-creature share) so the same ledger applies to them.

## Running

```sh
uv run crimson minds run --mind flank --minds 2 --seed 48879 --ticks 3600
uv run crimson minds run --mind llm --url http://127.0.0.1:8083 --model qwen2.5-7b-instruct --budget 3000 --price 10
uv run crimson minds run --mind random --ungoverned
uv run crimson minds verify <dir>/run.crd
uv run crimson minds compare --mind flank --seeds 3
```

`run` plays survival headlessly with an aim-bot pilot (`crimson.minds.pilot`), asks the minds every
`--cadence` ticks (default 15, so four times a second of game time), verifies the recording by
re-simulating it, and writes `run.crd`, `receipts.jsonl`, `run.minds.json` and, for a model, one
`transcript-<mind>.jsonl` per mind (prompt, reply, tokens, steers). `--mind idle` is the scripted baseline:
no steers, the native AI. `--ungoverned` meters the same things and refuses nothing the simulation would
accept. `compare` plays the scripted, ungoverned and governed conditions over the same seeds and prints
one table (kills, elapsed, steers, inferences, tokens, credits, refusals, verification).

Pool slots are dealt to the minds round-robin (`owner_of`), so each mind owns a share of every wave. A
mind sees its own creatures under `owned` and the nearest others under `others` (`crimson.minds.observe`).

## The ledger

`crimson.minds.ledger` is the control plane in miniature. Per mind: a budget, a balance, a price per
thousand tokens, and counters. Receipts, one line each in `receipts.jsonl`:

| `kind` | When |
| --- | --- |
| `inference` | a decision was charged (`tokens`, `cost`), or failed (`detail: failed`, no cost) |
| `steer` | a steer was recorded |
| `refusal` | the policy refused: `budget_exhausted` before an inference or a steer, or `slot N is not owned by mind M` |
| `invalid` | the simulation would not accept it: a dead slot or a point off the arena; never recorded |

Governed, a mind with no balance left is not asked at all, and a steer for a creature another mind owns
is refused. Ungoverned, nothing is refused; invalid steers are still dropped, since the replay must verify.

## Minds

| Mind | Decides |
| --- | --- |
| `idle` | nothing: the creatures keep their native AI |
| `random` | a uniform random arena point per owned creature, from its own seed |
| `flank` | around the player's aim: each creature to one side of the firing line or behind it, closing in when near |
| `llm` | an OpenAI-compatible chat-completions server (`llama-server`, or anything serving `/v1/chat/completions`): compact JSON observation in, `{"steers":[{"i":..,"x":..,"y":..}]}` out; lenient parsing, clamped to the arena, unknown indices dropped |

The source is `src/crimson/minds/`; the command type is `CreatureSteerCommand` in `src/crimson/sim/commands.py`,
the session rules in `src/crimson/sim/sessions.py`, the codec rules in `src/crimson/replay/codec.py`.
