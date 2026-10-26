"""A language model as a mind: an OpenAI-compatible chat-completions server decides the steers.

The server's reported token usage is what the ledger charges, never a declared number. The request
carries the mind's temperature and an optional seed; across servers, builds and weights a live run
differs, which is the point: the recording, not the model, is what replays.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

import msgspec

from .minds import Decision, Steer, clamp_arena
from .observe import Observation
from .tactics import MOVES, steer_for_move

SYSTEM_PROMPT_COORDS = (
    "You command monsters in Crimsonland, a top-down shooter. The arena is 0..1024 on both axes. "
    "The player stands at (player_x, player_y), aims at (player_aim_x, player_aim_y) and shoots along that line. "
    "Every few ticks (60 ticks = 1 s) you get the monsters you own under `owned` and the nearest others under `others`. "
    "Set a target point for each owned monster: it walks toward that point until your next order. "
    "Good tactics: close in from the sides and from behind the player's aim, spread out so one burst cannot kill "
    "several, and never queue up along the firing line. "
    'Reply with JSON only, no prose: {"steers":[{"i":<owned index>,"x":<0..1024>,"y":<0..1024>}, ...]}.'
)

# The model never gets `hold`: a monster that stands still is a free kill, and small models reach for it.
LLM_MOVES = tuple(move for move in MOVES if move != "hold")

SYSTEM_PROMPT_MOVES = (
    "You command monsters in Crimsonland, a top-down shooter, and you want the player dead. The player at "
    "(player_x, player_y) aims at (player_aim_x, player_aim_y) and shoots along that line; turning takes a moment. "
    "Every few ticks (60 ticks = 1 s) you get your monsters as `groups` (g = group id, n = how many, x/y = their "
    "centre, hp = their average health) and how many other monsters are near (`others`). Moves: flank_left and "
    "flank_right circle to that side of the player's aim and close in; behind gets round the back of the aim; rush "
    "goes straight at the player; spread steps out of the line of fire. Split the horde across sides so one burst "
    "cannot kill several, send healthy groups round and wounded ones straight in, and never send everyone down the "
    'firing line. Reply with JSON only, no prose, short: {"default":"<move for every group not listed>",'
    '"moves":[{"g":<group id>,"m":"<move>"}, ...]}, exceptions only.'
)

# Kept for callers that import the old name.
SYSTEM_PROMPT = SYSTEM_PROMPT_COORDS

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class Exchange(msgspec.Struct, frozen=True):
    """One inference as the model saw it, kept for the audit trail."""

    tick: int
    prompt: str
    reply: str
    prompt_tokens: int
    completion_tokens: int
    steers: int
    ok: bool
    error: str = ""
    # Seconds the call took, request to reply.
    latency_s: float = 0.0


def observation_prompt(observation: Observation) -> str:
    """The user message: the observation as compact JSON with integer coordinates (fewer tokens)."""

    def view(creature: Any) -> dict[str, int]:
        return {"i": creature.index, "x": round(creature.x), "y": round(creature.y), "hp": round(creature.hp)}


    payload = {
        "tick": observation.tick,
        "player_x": round(observation.player_x),
        "player_y": round(observation.player_y),
        "player_health": round(observation.player_health),
        "player_aim_x": round(observation.player_aim_x),
        "player_aim_y": round(observation.player_aim_y),
        "owned": [view(creature) for creature in observation.owned],
        "others": [view(creature) for creature in observation.others],
    }
    return json.dumps(payload, separators=(",", ":"))


def group_prompt(observation: Observation) -> str:
    """The user message in moves mode: the owned creatures as groups, the others as a count (fewer tokens)."""

    payload = {
        "tick": observation.tick,
        "player_x": round(observation.player_x),
        "player_y": round(observation.player_y),
        "player_health": round(observation.player_health),
        "player_aim_x": round(observation.player_aim_x),
        "player_aim_y": round(observation.player_aim_y),
        "groups": [{"g": g.id, "n": g.count, "x": round(g.x), "y": round(g.y), "hp": round(g.hp)} for g in observation.groups],
        "others": len(observation.others),
    }
    return json.dumps(payload, separators=(",", ":"))


def parse_steers(reply: str, observation: Observation) -> tuple[Steer, ...]:
    """The steers in a model reply: one per owned index it names, clamped to the arena; anything else is dropped."""

    match = _JSON_OBJECT_RE.search(reply)
    if match is None:
        return ()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ()
    items = data.get("steers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return ()
    owned = {creature.index for creature in observation.owned}
    arena = float(observation.arena)
    steers: dict[int, Steer] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["i"])
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if index not in owned or not (x == x and y == y):  # noqa: PLR0124 - NaN check without math.isnan on bools
            continue
        steers[index] = Steer(creature_index=index, target_x=clamp_arena(x, arena), target_y=clamp_arena(y, arena))
    return tuple(steers[index] for index in sorted(steers))


def parse_moves(reply: str, observation: Observation) -> tuple[Steer, ...]:
    """The steers a reply of moves resolves to: one per owned index naming a known move; anything else is dropped."""

    match = _JSON_OBJECT_RE.search(reply)
    if match is None:
        return ()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, dict):
        return ()
    owned = {creature.index: creature for creature in observation.owned}
    groups = {group.id: group for group in observation.groups}
    steers: dict[int, Steer] = {}
    default = data.get("default")
    if isinstance(default, str) and default.strip().lower() in LLM_MOVES:
        move = default.strip().lower()
        for index, creature in owned.items():
            steers[index] = steer_for_move(move, observation, creature)
    items = data.get("moves")
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            move = str(item["m"]).strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
        if move not in LLM_MOVES:
            continue
        indices: list[int] = []
        try:
            if "g" in item:
                group = groups.get(int(item["g"]))
                indices = list(group.members) if group is not None else []
            elif "i" in item:
                indices = [int(item["i"])]
        except (TypeError, ValueError):
            continue
        for index in indices:
            if index in owned:
                steers[index] = steer_for_move(move, observation, owned[index])
    return tuple(steers[index] for index in sorted(steers))


class LlmMind:
    """A chat-completions model behind the `Mind` protocol.

    `mode="moves"` (the default) asks for one move per creature from the tactics vocabulary; `mode="coords"`
    asks for arena points.
    """

    def __init__(
        self,
        name: str,
        *,
        url: str,
        model: str = "",
        temperature: float = 0.8,
        seed: int | None = None,
        max_tokens: int = 120,
        timeout_s: float = 60.0,
        api_key: str = "",
        json_mode: bool = True,
        mode: str = "moves",
    ) -> None:
        if mode not in ("moves", "coords"):
            raise ValueError(f"unknown LlmMind mode {mode!r}; use moves or coords")
        self.mode = mode
        self.name = str(name)
        self.model = str(model) or "chat-completions"
        self._url = str(url).rstrip("/")
        self._model = str(model)
        self._temperature = float(temperature)
        self._seed = seed
        self._max_tokens = int(max_tokens)
        self._timeout_s = float(timeout_s)
        self._api_key = str(api_key)
        self._json_mode = bool(json_mode)
        self.transcript: list[Exchange] = []
        self.failures = 0

    def _request(self, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_MOVES if self.mode == "moves" else SYSTEM_PROMPT_COORDS},
                {"role": "user", "content": prompt},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._model:
            body["model"] = self._model
        if self._seed is not None:
            body["seed"] = int(self._seed)
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._url}/v1/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def decide(self, observation: Observation) -> Decision:
        import time

        prompt = group_prompt(observation) if self.mode == "moves" else observation_prompt(observation)
        started = time.perf_counter()
        try:
            data = self._request(prompt)
            reply = str(data["choices"][0]["message"]["content"])
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            self.failures += 1
            error = f"{type(exc).__name__}: {exc}"
            self.transcript.append(
                Exchange(tick=observation.tick, prompt=prompt, reply="", prompt_tokens=0, completion_tokens=0, steers=0, ok=False, error=error, latency_s=round(time.perf_counter() - started, 3)),
            )
            return Decision(ok=False, note=error)
        steers = parse_moves(reply, observation) if self.mode == "moves" else parse_steers(reply, observation)
        self.transcript.append(
            Exchange(
                tick=observation.tick,
                prompt=prompt,
                reply=reply,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                steers=len(steers),
                ok=True,
                latency_s=round(time.perf_counter() - started, 3),
            ),
        )
        return Decision(steers=steers, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
