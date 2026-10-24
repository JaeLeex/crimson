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
uv run crimson minds run --mind flank --minds 2 --scene ring --seed 7
uv run crimson minds run --mind llm --url http://127.0.0.1:8081 --model qwen2.5-1.5b-instruct --budget 120 --price 10
uv run crimson minds run --mind random --ungoverned
uv run crimson minds verify <dir>/run.crd
uv run crimson minds compare --mind flank --scene ring --seeds 5
uv run crimson minds stochastic --mind llm --url http://127.0.0.1:8083 --reps 5 --seed 7
uv run crimson minds grid native/run.crd flank/run.crd surround/run.crd llm/run.crd --labels "native AI,flank,surround,Qwen 7B" --out grid.mp4
```

`run` plays a scene headlessly with an aim-bot pilot (`crimson.minds.pilot`), asks the minds every
`--cadence` ticks (default 15, four times a second of game time), verifies the recording by re-simulating it,
and writes `run.crd`, `receipts.jsonl`, `run.minds.json` and, for a model, one `transcript-<mind>.jsonl` per
mind (prompt, reply, tokens, steers). `--mind idle` is the scripted baseline: no steers, the native AI.
`--ungoverned` meters the same things and refuses nothing the simulation would accept. `compare` plays the
scripted, ungoverned and governed conditions over the same seeds and prints one table (outcome, health,
damage, kills, steers, inferences, tokens, credits, refusals, verification). `stochastic` plays **one** seed
many times with the same pilot and reports the spread and how many distinct recordings came out: the
stochastic-game test (below). `grid` renders two or four replays as one labelled video (side by side or 2×2)
through `crimson replay render`, Pillow for the labels and ffmpeg.

### Scenes

Survival opens with a trickle, which no tactic can make interesting, so the default scene is a dense custom
quest (`crimson.minds.arena`, built with the quest recipe compiler): assault rifle, no drops, four waves of
small gray aliens at 0, 14, 28 and 42 s. `--scene ring` spawns each wave on a ring round the player, where
the native chase already attacks from every side; `--scene arena` (`edge`) walks each wave in from one edge,
where the native chase arrives as a column down the line of fire. `--scene survival` is the stock mode.

### The pilot

A perfect aim bot takes no damage from any horde, so the pilot has a skill dial (`PilotSkill`): its aim moves
at most `--pilot-aim-lag` units per tick toward the nearest creature (default 40), fire comes in bursts of
`--pilot-fire-on` ticks with `--pilot-fire-off` ticks of rest (default 20/20), and it walks away from anything
within 150 units. The same pilot plays every condition of a comparison, so the horde's coordination is what
differs.

### Tactics

`crimson.minds.tactics` is a six-word vocabulary a mind can speak instead of inventing coordinates:
`flank_left`, `flank_right` (circle to that side of the player's aim, then close in), `behind`, `rush`,
`hold`, `spread` (step out of the line of fire). Each resolves to a point relative to the player and the
direction they aim. The `llm` mind speaks it by default (`--llm-mode moves`): it sees its creatures as
**groups** (an eighth of the circle round the player, near or far; `Observation.groups`) and answers with a
default move plus exceptions per group, which keeps a reply to a few dozen tokens whatever the horde's size. It
never gets `hold`: a monster that stands still is a free kill, and small models reach for it. `coords` asks for
arena points instead. The `flank` heuristic deals each creature a side once and keeps it; `surround` puts each
creature on an evenly spaced slot of a ring and, once enough are in place, rushes with all of them.

### The stochastic-game test

The simulation is deterministic and a recording replays bit for bit, so where does a different game come
from? From the minds. `crimson minds stochastic` plays one run seed, one pilot, N times; a sampling mind (a model
at temperature, a heuristic on a fresh seed each time) makes N different games, each of which verifies. With
`--llm-seed` fixed and temperature 0 the model server is deterministic on the same build, and N recordings
come out identical: the test that the variation is the mind's and nothing else's. With the flank heuristic on
the ring, seed 7, ten games: ten distinct recordings, the pilot dead in six, alive between 23 and 60 s.

### What it shows

Ten seeds per condition, 90 s cap, the default pilot:

| scene | mind | pilot deaths | mean alive s | mean kills | mean damage |
| --- | --- | ---: | ---: | ---: | ---: |
| ring | idle (native AI) | 3/10 | 59.3 | 84.1 | 42 |
| ring | flank | 8/10 | 42.9 | 58.6 | 85 |
| ring | surround | 5/10 | 50.8 | 72.9 | 65 |
| ring | random | 0/10 | 71.7 | 94.3 | 4 |
| edge | idle (native AI) | 6/10 | 58.7 | 77.9 | 65 |
| edge | flank | 2/10 | 67.2 | 88.3 | 30 |
| edge | surround | 0/10 | 74.1 | 94.6 | 34 |
| edge | random | 0/10 | 77.7 | 96.0 | 30 |

A language model on the same ring seed 7, two minds asked once a second (`--cadence 60`), moves mode, 60 s cap;
the governed minds had 120 credits each at 10 per thousand tokens:

| model | condition | pilot health | kills | steers | inferences | tokens | refused | wall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen2.5-7B-Instruct | ungoverned | 100 | 81 | 1,176 | 118 | 88,148 | 0 | 17.5 min |
| Qwen2.5-7B-Instruct | governed | 100 | 84 | 244 | 38 | 23,807 | 99 | 4.1 min |
| Qwen2.5-1.5B-Instruct | ungoverned | 90 | 90 | 259 | 107 | 87,371 | 0 | 8.4 min |
| Qwen2.5-1.5B-Instruct | governed | 30 | 85 | 67 | 32 | 23,721 | 80 | 2.1 min |

The 7B model answers well (nine or ten steers per reply, a mix of `behind`, `flank_left`, `flank_right` and
`rush` as exceptions) but picks `hold` as the default move half the time, so half its horde stands still and
dies; the pilot takes no damage. The 1.5B model's replies were half unparsable and otherwise `hold`. Governed,
both ran out of credits after about sixteen asks per mind and the native AI took the horde back (the
refusals), which with the 1.5B model is when the pilot got hurt. So, on this scene, a two-line flank rule is a
better general than a small local model, and the model's value here is the governed inference itself:
charged on tokens, cut off at the budget, falling back, and recorded. Every run above verifies.

On the ring the flanking horde kills a pilot the native chase mostly cannot: the pilot kites away from the
nearest creature, which beats a crowd that all comes straight in, and does not beat one that splits round the
sides. From an edge the native column is the deadlier shape, and circling only gives the pilot time. Random
steering is the control: it scatters the horde and the pilot lives. A mind, then, is a measurable change in
what the horde does, in either direction, under a budget that binds (governed runs show the refusals) and in a
recording anyone can replay.

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
| `flank` | each creature is dealt one side of the player's aim (left, right or behind) and keeps it; it rushes once near |
| `surround` | each creature takes a slot on a ring round the player; once enough are in place, all rush |
| `llm` | an OpenAI-compatible chat-completions server (`llama-server`, or anything serving `/v1/chat/completions`): compact JSON observation in; `{"moves":[{"i":..,"m":"flank_left"}]}` out (or `{"steers":[{"i":..,"x":..,"y":..}]}` in `coords` mode); lenient parsing, clamped to the arena, unknown indices and moves dropped |

The source is `src/crimson/minds/`; the command type is `CreatureSteerCommand` in `src/crimson/sim/commands.py`,
the session rules in `src/crimson/sim/sessions.py`, the codec rules in `src/crimson/replay/codec.py`.
