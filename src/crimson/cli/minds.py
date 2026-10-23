"""`crimson minds`: run, verify and compare runs whose creatures are steered by minds (docs/rewrite/minds.md)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import msgspec
import typer

from ..paths import default_runtime_dir

if TYPE_CHECKING:
    from ..minds.driver import BoundMind, MindsRun

minds_app = typer.Typer(add_completion=False, help="Creatures steered by minds: scripted, random, heuristic or a language model.")

MindKind = Literal["idle", "random", "flank", "llm"]
Condition = Literal["scripted", "ungoverned", "governed"]

_BASE_DIR = typer.Option(
    default_runtime_dir(),
    "--base-dir",
    "--runtime-dir",
    help="base path for runtime files (default: per-user OS data dir; override with CRIMSON_RUNTIME_DIR)",
)


def _build_minds(
    kind: str,
    *,
    count: int,
    seed: int,
    budget: int | None,
    price: int,
    url: str,
    model: str,
    temperature: float,
    llm_seed: int | None,
    timeout_s: float,
) -> list[BoundMind]:
    from ..minds.driver import BoundMind
    from ..minds.llm import LlmMind
    from ..minds.minds import FlankMind, IdleMind, RandomMind

    bound: list[BoundMind] = []
    for index in range(max(1, int(count))):
        mind_seed = (int(seed) * 1_000_003 + index) & 0xFFFFFFFF
        match kind:
            case "idle":
                mind = IdleMind()
            case "random":
                mind = RandomMind(mind_seed)
            case "flank":
                mind = FlankMind(mind_seed)
            case "llm":
                mind = LlmMind(
                    f"llm-{index}",
                    url=url,
                    model=model,
                    temperature=temperature,
                    seed=None if llm_seed is None else int(llm_seed) + index,
                    timeout_s=timeout_s,
                )
            case _:
                typer.echo(f"unknown mind kind {kind!r}; use idle, random, flank or llm", err=True)
                raise typer.Exit(code=2)
        bound.append(BoundMind(mind=mind, budget=budget, price_per_1k_tokens=int(price)))
    return bound


def _verify(run: MindsRun) -> tuple[bool, list[str]]:
    """Re-simulate the finished replay from its bytes and compare with the result it recorded."""

    from ..replay.codec import dump_replay, load_replay
    from ..replay.driver.playback_driver import PlaybackDriver
    from ..sim.run_result import run_result_mismatches

    replay = load_replay(dump_replay(run.replay))
    result = PlaybackDriver(replay, warn_on_version_mismatch=False).run()
    mismatches = run_result_mismatches(replay.result, result)
    return not mismatches, mismatches


def _write_run(run: MindsRun, out_dir: Path, *, condition: str, kind: str, verified: bool, mismatches: list[str]) -> Path:
    from ..replay.codec import dump_replay_file

    out_dir.mkdir(parents=True, exist_ok=True)
    replay_path = out_dir / "run.crd"
    dump_replay_file(replay_path, run.replay)
    (out_dir / "receipts.jsonl").write_bytes(b"".join(msgspec.json.encode(receipt) + b"\n" for receipt in run.receipts))
    for mind_id, exchanges in run.transcripts.items():
        (out_dir / f"transcript-{mind_id}.jsonl").write_bytes(b"".join(msgspec.json.encode(item) + b"\n" for item in exchanges))
    meta = {
        "condition": condition,
        "mind": kind,
        "verified": verified,
        "mismatches": mismatches,
        "replay": replay_path.name,
        "receipts": "receipts.jsonl",
        **run.summary(),
    }
    (out_dir / "run.minds.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return replay_path


def _progress_printer(every: int):
    last = {"tick": 0}

    def report(tick: int, total: int) -> None:
        if tick - last["tick"] >= every or tick == total:
            last["tick"] = tick
            typer.echo(f"  tick {tick}/{total}", err=True)

    return report


def _summary_line(label: str, run: MindsRun, *, verified: bool) -> str:
    account_bits = " ".join(
        f"[{account.name}: inferences={account.inferences} tokens={account.tokens} spent={account.spent} "
        f"steers={account.steers} refused={account.refusals} invalid={account.invalid}"
        + ("" if account.budget is None else f" left={account.balance}")
        + "]"
        for account in run.accounts
    )
    return (
        f"{label}: outcome={run.result.outcome} ticks={run.ticks} elapsed_ms={run.result.elapsed_ms} "
        f"kills={run.result.kills} xp={run.result.players[0].experience} steers={run.steers_recorded} "
        f"verified={'yes' if verified else 'NO'} wall={run.wall_s:.1f}s minds={run.mind_s:.1f}s {account_bits}"
    )


@minds_app.command("run")
def cmd_minds_run(
    mind: str = typer.Option("flank", "--mind", help="idle (native AI, the scripted baseline), random, flank or llm"),
    minds: int = typer.Option(2, "--minds", help="how many minds share the horde (slots dealt round-robin)"),
    seed: int = typer.Option(0xBEEF, "--seed", help="uint32 run seed"),
    ticks: int = typer.Option(3600, "--ticks", help="ticks to play at most (60 per second)"),
    cadence: int = typer.Option(15, "--cadence", help="ask the minds every N ticks"),
    hold: int | None = typer.Option(None, "--hold", help="ticks a steer holds (default: the cadence)"),
    budget: int | None = typer.Option(1000, "--budget", help="credits per mind; omit with --ungoverned for no limit"),
    price: int = typer.Option(10, "--price", help="credits per 1k tokens"),
    ungoverned: bool = typer.Option(False, "--ungoverned", help="meter but refuse nothing: no budget, no ownership"),
    url: str = typer.Option("http://127.0.0.1:8081", "--url", help="chat-completions server for --mind llm"),
    model: str = typer.Option("", "--model", help="model name for servers that route by name"),
    temperature: float = typer.Option(0.8, "--temperature", help="sampling temperature for --mind llm"),
    llm_seed: int | None = typer.Option(None, "--llm-seed", help="sampling seed for --mind llm (per mind: seed + index)"),
    timeout_s: float = typer.Option(60.0, "--timeout", help="seconds to wait for a model reply"),
    out: Path | None = typer.Option(None, "--out", help="output directory (default: <base-dir>/minds/<timestamp>-<seed>)"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Play one minds run headlessly, verify it, and write run.crd, receipts.jsonl and run.minds.json."""
    from ..minds.driver import MindsRunConfig, run_minds

    governed = not ungoverned
    bound = _build_minds(
        mind,
        count=minds,
        seed=seed,
        budget=None if ungoverned else budget,
        price=price,
        url=url,
        model=model,
        temperature=temperature,
        llm_seed=llm_seed,
        timeout_s=timeout_s,
    )
    config = MindsRunConfig(seed=seed, ticks=ticks, cadence=cadence, hold=hold, governed=governed)
    condition = "scripted" if mind == "idle" else ("governed" if governed else "ungoverned")
    typer.echo(f"minds run: {condition} mind={mind} x{len(bound)} seed={seed} ticks<={ticks} cadence={cadence}", err=True)
    run = run_minds(bound, config, progress=_progress_printer(600))
    verified, mismatches = _verify(run)
    if out is None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        out = base_dir / "minds" / f"{stamp}-{seed}"
    replay_path = _write_run(run, out, condition=condition, kind=mind, verified=verified, mismatches=mismatches)
    typer.echo(_summary_line(condition, run, verified=verified))
    typer.echo(f"wrote {replay_path} (+ receipts.jsonl, run.minds.json); play it with: crimson replay play {replay_path}")
    if not verified:
        typer.echo(f"replay did not verify: {', '.join(mismatches)}", err=True)
        raise typer.Exit(code=3)


@minds_app.command("verify")
def cmd_minds_verify(
    replay_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="a run.crd written by `crimson minds run`"),
) -> None:
    """Re-simulate a steered replay and check the result it recorded (the same check as `crimson replay verify`)."""
    from ..game_version import MINDS_RULES
    from ..replay.codec import ReplayCodecError, load_replay_file
    from ..replay.driver.playback_driver import PlaybackDriver, ReplayRunnerError
    from ..sim.commands import CreatureSteerCommand
    from ..sim.run_result import run_result_mismatches

    try:
        replay = load_replay_file(replay_file)
        result = PlaybackDriver(replay, warn_on_version_mismatch=False).run()
    except (ReplayCodecError, ReplayRunnerError) as exc:
        typer.echo(f"verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    steers = sum(isinstance(command, CreatureSteerCommand) for tick in replay.ticks for command in tick.commands)
    mismatches = run_result_mismatches(replay.result, result)
    status = "ok" if not mismatches else "result_mismatch"
    typer.echo(
        f"{status}: rules={replay.rules}{' (minds)' if replay.rules == MINDS_RULES else ''} ticks={len(replay.ticks)} "
        f"steers={steers} outcome={result.outcome} elapsed_ms={result.elapsed_ms} kills={result.kills} "
        f"rng_state={result.rng_state}"
        + (f"; mismatches={','.join(mismatches)}" if mismatches else ""),
    )
    if mismatches:
        raise typer.Exit(code=2)


@minds_app.command("compare")
def cmd_minds_compare(
    mind: str = typer.Option("flank", "--mind", help="the mind for the ungoverned and governed conditions: random, flank or llm"),
    minds: int = typer.Option(2, "--minds", help="how many minds share the horde"),
    seeds: int = typer.Option(3, "--seeds", help="how many seeds to play per condition"),
    seed: int = typer.Option(0xBEEF, "--seed", help="first seed; the rest follow it"),
    ticks: int = typer.Option(1800, "--ticks", help="ticks to play at most per run"),
    cadence: int = typer.Option(15, "--cadence", help="ask the minds every N ticks"),
    budget: int = typer.Option(300, "--budget", help="credits per mind in the governed condition"),
    price: int = typer.Option(10, "--price", help="credits per 1k tokens"),
    url: str = typer.Option("http://127.0.0.1:8081", "--url", help="chat-completions server for --mind llm"),
    model: str = typer.Option("", "--model"),
    temperature: float = typer.Option(0.8, "--temperature"),
    timeout_s: float = typer.Option(60.0, "--timeout"),
    out: Path | None = typer.Option(None, "--out", help="output directory (default: <base-dir>/minds/compare-<timestamp>)"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Play scripted, ungoverned and governed conditions over the same seeds and print one table."""
    from ..minds.driver import MindsRunConfig, run_minds

    if out is None:
        out = base_dir / "minds" / f"compare-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    for offset in range(max(1, int(seeds))):
        run_seed = (int(seed) + offset) & 0xFFFFFFFF
        for condition in ("scripted", "ungoverned", "governed"):
            kind = "idle" if condition == "scripted" else mind
            governed = condition == "governed"
            bound = _build_minds(
                kind,
                count=minds,
                seed=run_seed,
                budget=budget if governed else None,
                price=price,
                url=url,
                model=model,
                temperature=temperature,
                llm_seed=None,
                timeout_s=timeout_s,
            )
            typer.echo(f"  {condition:<10} seed={run_seed} ...", err=True)
            run = run_minds(bound, MindsRunConfig(seed=run_seed, ticks=ticks, cadence=cadence, governed=governed))
            verified, mismatches = _verify(run)
            _write_run(run, out / f"{condition}-{run_seed}", condition=condition, kind=kind, verified=verified, mismatches=mismatches)
            accounts = run.accounts
            rows.append(
                {
                    "condition": condition,
                    "seed": run_seed,
                    "ticks": run.ticks,
                    "outcome": str(run.result.outcome),
                    "elapsed_ms": run.result.elapsed_ms,
                    "kills": run.result.kills,
                    "xp": run.result.players[0].experience,
                    "steers": run.steers_recorded,
                    "inferences": sum(account.inferences for account in accounts),
                    "tokens": sum(account.tokens for account in accounts),
                    "spent": sum(account.spent for account in accounts),
                    "refused": sum(account.refusals for account in accounts),
                    "invalid": sum(account.invalid for account in accounts),
                    "verified": verified,
                    "wall_s": round(run.wall_s, 2),
                    "mind_s": round(run.mind_s, 2),
                },
            )
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    columns = ("condition", "seed", "ticks", "outcome", "elapsed_ms", "kills", "xp", "steers", "inferences", "tokens", "spent", "refused", "invalid", "verified", "wall_s", "mind_s")
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in columns}
    typer.echo("  ".join(column.ljust(widths[column]) for column in columns))
    for row in rows:
        typer.echo("  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    typer.echo(f"wrote {out / 'compare.json'} and one directory per run ({time.perf_counter() - started:.1f}s)")
