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
the ring, seed 7, ten games (cadence 15, 60 s cap): ten distinct recordings, the pilot dead in six, alive between
23 and 60 s; the surround heuristic, eight dead, alive 8 to 60 s; random steering, none dead.

Qwen2.5-7B-Instruct as one general over the whole horde, groups mode, asked twice a second, 40 s cap, ring seed 7:

| temperature | model seed | games | distinct recordings | pilot health at the end | kills | all verified |
| --- | --- | ---: | ---: | --- | --- | --- |
| 0.9 | none | 4 | 4 | 90, 0, 100, 80 | 52, 59, 57, 65 | yes |
| 0 | fixed | 2 | 1 | 100, 100 | 22, 22 | yes |

Four games, four different hordes: in one the pilot ends on its last hit point, in another untouched. The two
games at temperature zero with a fixed sampling seed are byte-identical recordings, so the variation is the
model's sampling and nothing else; the simulation, the pilot and the recording contribute none. Each call took
about 6.6 s on an M1 iMac (the prompt is ~430 tokens, most of it the system prompt), so a 40 s game takes
8 to 10 minutes of wall time; the recording plays in real time.

Governed, the same model as two minds with 120 credits each (ring seed 7, 40 s): 49 inferences, 21,742 tokens,
244 credits spent, then 111 refusals as the budgets ran dry and the native AI took the horde back; the
ungoverned pair made 142 inferences on 66,139 tokens. Both pilots ended untouched on that seed; both runs verify.

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
| `jev` | TypeSafe's Jev (System One): one Choice question per group over the five moves, the whole horde in one request (~0.11 s); the mind samples each group's move from Jev's probabilities with its own seed and `--temperature` (0 = top move) |
| `llm` | an OpenAI-compatible chat-completions server (`llama-server`, or anything serving `/v1/chat/completions`): compact JSON observation in; `{"moves":[{"i":..,"m":"flank_left"}]}` out (or `{"steers":[{"i":..,"x":..,"y":..}]}` in `coords` mode); lenient parsing, clamped to the arena, unknown indices and moves dropped |

The source is `src/crimson/minds/`; the command type is `CreatureSteerCommand` in `src/crimson/sim/commands.py`,
the session rules in `src/crimson/sim/sessions.py`, the codec rules in `src/crimson/replay/codec.py`.

## Jev

`--mind jev` asks TypeSafe's Jev (`POST https://api.typesafe.ai/v1/systemone`, key in `TYPESAFE_API_KEY`) one
multiple-choice question per group and samples the moves from the returned probabilities (`crimson.minds.jev`).
Ring scene, two minds asked four times a second (2026-10-26):

- **Speed.** Median 0.11 s per call (max ~0.2 s), so a 60 s game plays in about 30 s: faster than real time.
  About 750,000 input tokens per 60 s game for two minds, roughly $0.03 at the published $0.042 per million.
- **Stochastic test, seed 7, ten games each:**

  | temperature | distinct recordings | pilot deaths | pilot health, mean |
  | --- | ---: | ---: | ---: |
  | 0.5 | 10 | 2 | 40 |
  | 1.0 | 10 | 0 | 86 |
  | 2.0 | 10 | 2 | 59 |

- **Not repeatable at the source.** Three games at temperature 0 (always the top move) still differed. Jev's
  probabilities move a few points between identical requests, and on these states it is close to indifferent
  (every move 0.2–0.3), so the top move flips between near-ties from the second decision on. Reproducibility
  therefore lives in the recording, not in re-asking the model; every game above verifies.
- **Against the native AI, ten seeds (100–109), 90 s:** native AI killed the pilot 3/10 (mean damage 42);
  ungoverned Jev 0/10 (mean damage 15); governed Jev (2,000 credits per mind, exhausted half way, native AI after)
  2/10 (mean damage 49). Ungoverned, Jev's near-even odds make the horde behave close to the random mind: it
  scatters, and the pilot does better than against the native chase.

## Settling on Nunchi

`--nunchi` settles a run on a Nunchi settlement chain: a four-validator Commonware chain on the Nunchi SDK's
settlement module, driven by the `nunchi-minds` binary (repo `nunchi-minds`; `crimson.minds.nunchi` calls it).

- **Admit, before play.** Each mind is a principal `crimson/<run id>/mind-<n>`. The chain grants it permission
  to use its model, issues its budget, and records its lease (run, mind, model, budget, price, seed; written
  beside the replay as `lease-mind-<n>.json`) as an authorization keyed by the lease's SHA-256. The game's
  ledger enforces the budget the chain issued.
- **During play.** Every 300 ticks the game reads each mind's permission; a mind the operator revoked is refused
  from then on and its creatures fall back to the native AI. `--revoke-at T --revoke-mind N` demonstrates the
  switch by revoking on chain at tick T.
- **Settle, after play.** Each mind's spend is debited on chain, and a usage record (inference cost in micro-USD,
  Jev at its published $0.042 per million tokens) commits to the receipt
  `crimson-minds/v1 replay=<sha256 of the replay payload> receipts=<sha256 of receipts.jsonl> principal=<p>`.
  The chain will not debit past the balance: an overspent mind settles with `settled: false` and its balance
  whole, and its usage is still recorded.
- **Audit.** `crimson minds audit <run dir>` replays the recording, recomputes both digests and the receipt,
  re-adds each mind's spend from the receipt log, and reads the lease authorization, balance and usage back from
  the chain (17 checks for two minds).

Ring seed 7, two Jev minds, 30 s, temperature 0.5 (2026-10-26):

| scenario | mind 0 | mind 1 | audit |
| --- | --- | --- | --- |
| governed, 300 credits each | spent 297, debited, 3 left | spent 294, debited, 6 left | 17/17 |
| ungoverned, 300 issued on chain | spent 1,966, refused, 300 left | spent 1,811, refused, 300 left | 17/17 |
| governed, 1,500 each, operator revokes mind 0 at tick 600 | last call tick 585, spent 789, 711 left | ran to its budget at tick 1245 | 17/17 |

The first governed run on chain overspent (324 and 315 against 300): the ledger charged after the call and
refused only once the balance was already gone, so the last call overshot. The chain refused both debits. The
ledger now reserves the dearest call seen before asking a governed mind again.

## Metering video

`crimson minds grid` draws the metering on top of runs that carry `run.minds.json` and `receipts.jsonl`
(`--no-meters` turns it off):

- **Who controls each enemy.** A creature a mind is steering is drawn in that mind's colour (mind 0 orange,
  mind 1 cyan) with a ring of the same colour; native creatures keep their own colour, so a capped or revoked
  mind's enemies visibly turn back. This is render-only: `crimson.render.world.overrides` holds the colours the
  creature draw uses for those slots, and the simulation's own `CreatureState.tint` (read by fire damage and
  corpse decals) is never touched. Who is steering comes from the recorded commands (`steer_schedule`), because
  the session's `steer_holds` holds only what carries into the next tick.
- **The metering strip** along the bottom of each tile: the run's AI cost so far (dollars at the model's
  published price, or "$0 API (self-hosted)"); one bar per mind of credits left on its chain budget, with
  ACTIVE / BUDGET CAP / REVOKED ON CHAIN / OVER CAP +N, and a red overdraft bar for an ungoverned mind past its
  cap; the latest model calls (model, call number, tokens, latency, credits, dollars); the latest event.
- **Cards.** `--title`/`--subtitle` put a 4 s title card with the colour legend first; for runs settled on
  Nunchi a 7 s end card follows with each mind's on-chain debit (block, transaction) or the chain's refusal,
  revocations, the AI metered, and the audit result, which `audit_run` computes against the chain at render time.

`run_replay_render_video` takes the two hooks this uses, `before_draw` and `on_frame`; both default to none.
