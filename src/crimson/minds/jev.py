"""Jev (TypeSafe's System One model) as a mind: one multiple-choice question per group, answered in one call.

Jev does not write text. It picks from the options it is given and returns a probability for each, so the
tactics vocabulary maps onto it directly: every group of owned creatures is one Choice question over the five
moves, and the whole horde goes in one request (~0.1 s). The mind then **samples** each group's move from Jev's
probabilities with its own seeded stream, so the randomness is explicit and reproducible: `temperature` 0 takes
the most likely move, 1 samples Jev's odds as given, higher flattens them. The ledger charges Jev's reported
input tokens (output tokens are free).

API: POST https://api.typesafe.ai/v1/systemone with `Authorization: Bearer $TYPESAFE_API_KEY`
(docs.typesafe.ai/api). The key never leaves the environment and is never written to a transcript.
"""

from __future__ import annotations

import json
import math
import os
import random
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Any

import msgspec

from .minds import Decision, Steer
from .observe import GroupView, Observation
from .tactics import steer_for_move

JEV_URL = "https://api.typesafe.ai/v1/systemone"

# The model is never offered `hold`: a monster that stands still is a free kill.
JEV_MOVES: dict[str, str] = {
    "flank_left": "circle round the left of the player's line of fire, then close in",
    "flank_right": "circle round the right of the player's line of fire, then close in",
    "behind": "get round behind where the player is aiming and attack from the back",
    "rush": "charge straight at the player",
    "spread": "step sideways out of the player's line of fire",
}

INSTRUCTIONS = (
    "You command the monsters in group `groups[{i}]` (n = how many, x/y = where they are, hp = their average "
    "health) and you want the player dead. The player stands at `player_x`,`player_y` and shoots along the line "
    "toward `player_aim_x`,`player_aim_y`. Which move should this group make now? A group in the line of fire "
    "should get out of it or come round the side; a group already close or badly hurt should commit; the horde "
    "should come from several sides at once, not queue up on one."
)


class JevExchange(msgspec.Struct, frozen=True):
    """One Jev call as the audit trail keeps it: the groups asked, Jev's odds, and the moves the mind drew."""

    tick: int
    groups: int
    input_tokens: int
    latency_s: float
    ok: bool
    probabilities: dict[str, dict[str, float]] = {}
    moves: dict[str, str] = {}
    error: str = ""


def sample_move(
    probabilities: dict[str, float], *, temperature: float, rng: random.Random, vocabulary: Iterable[str] = JEV_MOVES,
) -> str:
    """A move drawn from Jev's odds: the most likely at temperature 0, the odds as given at 1, flatter above.

    `vocabulary` fixes the option order (ties and draws follow it); the steering moves by default.
    """

    options = [move for move in vocabulary if move in probabilities]
    if not options:
        raise ValueError("no known move in the answer")
    if temperature <= 0.0:
        # Ties break in vocabulary order, so temperature 0 is reproducible.
        return max(options, key=lambda move: (probabilities[move], -options.index(move)))
    weights = [max(float(probabilities[move]), 1e-9) ** (1.0 / temperature) for move in options]
    total = math.fsum(weights)
    roll = rng.random() * total
    for move, weight in zip(options, weights, strict=True):
        roll -= weight
        if roll <= 0.0:
            return move
    return options[-1]


def jev_state(observation: Observation, groups: tuple[GroupView, ...]) -> dict[str, Any]:
    return {
        "player_x": round(observation.player_x),
        "player_y": round(observation.player_y),
        "player_health": round(observation.player_health),
        "player_aim_x": round(observation.player_aim_x),
        "player_aim_y": round(observation.player_aim_y),
        "groups": [{"g": group.id, "n": group.count, "x": round(group.x), "y": round(group.y), "hp": round(group.hp)} for group in groups],
        "other_monsters_near": len(observation.others),
    }


def jev_questions(groups: tuple[GroupView, ...]) -> dict[str, Any]:
    return {
        f"g{group.id}": {"type": "choice", "instructions": INSTRUCTIONS.format(i=index), "criteria": JEV_MOVES}
        for index, group in enumerate(groups)
    }


class JevMind:
    """Jev picks a move per group; the mind samples it from Jev's probabilities with its own seed."""

    def __init__(
        self,
        name: str = "jev",
        *,
        seed: int = 0,
        temperature: float = 1.0,
        model: str = "jev-latest",
        url: str = JEV_URL,
        api_key: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.name = str(name)
        self.model = str(model)
        self._rng = random.Random(int(seed))
        self._temperature = float(temperature)
        self._url = str(url)
        self._api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        self._timeout_s = float(timeout_s)
        self.transcript: list[JevExchange] = []
        self.failures = 0

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise OSError("TYPESAFE_API_KEY is not set")
        request = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def decide(self, observation: Observation) -> Decision:
        import time

        groups = observation.groups
        if not groups:
            return Decision()
        body = {"model": self.model, "state": jev_state(observation, groups), "questions": jev_questions(groups)}
        started = time.perf_counter()
        try:
            data = self._request(body)
            answers = data["answers"]
            input_tokens = int((data.get("usage") or {}).get("input_tokens", 0))
            probabilities = {key: {move: float(p) for move, p in answer["probabilities"].items()} for key, answer in answers.items()}
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self.failures += 1
            error = f"{type(exc).__name__}: {exc}"
            self.transcript.append(
                JevExchange(tick=observation.tick, groups=len(groups), input_tokens=0, latency_s=time.perf_counter() - started, ok=False, error=error),
            )
            return Decision(ok=False, note=error)
        latency = time.perf_counter() - started
        owned = {creature.index: creature for creature in observation.owned}
        steers: list[Steer] = []
        moves: dict[str, str] = {}
        # Groups in id order, so the mind's draws are in a fixed order whatever order the answers came in.
        for group in groups:
            odds = probabilities.get(f"g{group.id}")
            if not odds:
                continue
            try:
                move = sample_move(odds, temperature=self._temperature, rng=self._rng)
            except ValueError:
                continue
            moves[f"g{group.id}"] = move
            steers.extend(steer_for_move(move, observation, owned[index]) for index in group.members if index in owned)
        self.transcript.append(
            JevExchange(
                tick=observation.tick,
                groups=len(groups),
                input_tokens=input_tokens,
                latency_s=round(latency, 4),
                ok=True,
                probabilities=probabilities,
                moves=moves,
            ),
        )
        steers.sort(key=lambda steer: steer.creature_index)
        return Decision(steers=tuple(steers), prompt_tokens=input_tokens, completion_tokens=0)
