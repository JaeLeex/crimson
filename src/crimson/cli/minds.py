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

# Jev's published price, US dollars per million input tokens (docs.typesafe.ai/models); output tokens are free.
JEV_USD_PER_MTOK = 0.042

minds_app = typer.Typer(add_completion=False, help="Creatures steered by minds: scripted, random, heuristic or a language model.")

MindKind = Literal["idle", "random", "flank", "surround", "llm"]
Condition = Literal["scripted", "ungoverned", "governed"]

_BASE_DIR = typer.Option(
    default_runtime_dir(),
    "--base-dir",
    "--runtime-dir",
    help="base path for runtime files (default: per-user OS data dir; override with CRIMSON_RUNTIME_DIR)",
)


def _nunchi(enabled: bool, rpc: str):
    if not enabled:
        return None
    from ..minds.nunchi import NunchiChain, NunchiError

    chain = NunchiChain(rpc=rpc)
    try:
        chain._run("chain")
    except NunchiError as exc:
        typer.echo(f"Nunchi chain not reachable at {rpc}: {exc}\nstart it with ~/nunchi-minds/start-chain.sh", err=True)
        raise typer.Exit(code=1) from exc
    return chain


def _settlement_line(run: MindsRun) -> str:
    if not run.settlement:
        return ""
    parts = []
    for entry in run.settlement["minds"]:
        settled = entry["settlement"]
        verdict = "settled" if settled["settled"] else f"REFUSED by chain ({settled['refusal']})"
        parts.append(
            f"{entry['principal']}: {verdict}, spent {settled['spent']}, balance {settled['balance_before']} -> {settled['balance_after']}, "
            f"usage {settled['usage_cost_usd_micros_total']} micro-USD",
        )
    events = "; ".join(f"tick {e['tick']}: {e['event']} (mind {e['mind']})" for e in run.settlement["events"])
    return "nunchi: " + " | ".join(parts) + (f" | events: {events}" if events else "")


def _chain_cell(run: MindsRun) -> str:
    """One word for a table: whether every mind settled on chain, or which the chain refused."""

    if not run.settlement:
        return "-"
    refused = [entry["mind"] for entry in run.settlement["minds"] if not entry["settlement"]["settled"]]
    return "settled" if not refused else "refused:" + ",".join(str(mind) for mind in refused)


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
    llm_mode: str = "moves",
) -> list[BoundMind]:
    from ..minds.driver import BoundMind
    from ..minds.jev import JevMind
    from ..minds.llm import LlmMind
    from ..minds.minds import FlankMind, IdleMind, RandomMind, SurroundMind

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
            case "surround":
                mind = SurroundMind(mind_seed)
            case "jev":
                mind = JevMind(f"jev-{index}", seed=mind_seed, temperature=temperature, model=model or "jev-latest", timeout_s=timeout_s)
            case "llm":
                mind = LlmMind(
                    f"llm-{index}",
                    url=url,
                    model=model,
                    temperature=temperature,
                    seed=None if llm_seed is None else int(llm_seed) + index,
                    timeout_s=timeout_s,
                    mode=llm_mode,
                )
            case _:
                typer.echo(f"unknown mind kind {kind!r}; use idle, random, flank, surround, jev or llm", err=True)
                raise typer.Exit(code=2)
        usd_per_mtok = JEV_USD_PER_MTOK if kind == "jev" else 0.0
        bound.append(BoundMind(mind=mind, budget=budget, price_per_1k_tokens=int(price), usd_per_mtok=usd_per_mtok))
    return bound


def _load_quest(path: Path):
    from ..quests.dsl import build_custom_quest

    try:
        return build_custom_quest(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"cannot load quest {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


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
    # Exactly the bytes the settlement receipt hashed.
    (out_dir / "receipts.jsonl").write_bytes(run.receipts_jsonl)
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
        f"health={run.player_health:.0f} kills={run.result.kills} xp={run.result.players[0].experience} steers={run.steers_recorded} "
        f"verified={'yes' if verified else 'NO'} wall={run.wall_s:.1f}s minds={run.mind_s:.1f}s {account_bits}"
    )


@minds_app.command("run")
def cmd_minds_run(
    mind: str = typer.Option("flank", "--mind", help="idle (native AI, the scripted baseline), random, flank, surround, jev (TypeSafe) or llm"),
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
    llm_mode: str = typer.Option("moves", "--llm-mode", help="moves (a tactic per creature) or coords (arena points)"),
    scene: str = typer.Option("arena", "--scene", help="arena (= edge: waves from one edge), ring (waves around the player) or survival"),
    others: int = typer.Option(12, "--others", help="nearest other creatures shown to a mind"),
    pilot_aim_lag: float = typer.Option(40.0, "--pilot-aim-lag", help="units per tick the pilot's aim may move (0 = snap)"),
    pilot_fire_on: int = typer.Option(20, "--pilot-fire-on", help="ticks the pilot holds fire per burst"),
    pilot_fire_off: int = typer.Option(20, "--pilot-fire-off", help="ticks the pilot rests between bursts"),
    out: Path | None = typer.Option(None, "--out", help="output directory (default: <base-dir>/minds/<timestamp>-<seed>)"),
    nunchi: bool = typer.Option(False, "--nunchi/--no-nunchi", help="admit and settle the minds on the Nunchi chain"),
    nunchi_rpc: str = typer.Option("http://127.0.0.1:47100", "--nunchi-rpc"),
    chain_budget: int = typer.Option(2000, "--chain-budget", help="credits issued on chain to a mind without a local budget"),
    revoke_at: int | None = typer.Option(None, "--revoke-at", help="demo: the operator revokes a mind on chain at this tick"),
    revoke_mind: int = typer.Option(0, "--revoke-mind"),
    quest: Path | None = typer.Option(None, "--quest", help="play this quest recipe (.quest) instead of --scene, e.g. one a builder wrote"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Play one minds run headlessly, verify it, and write run.crd, receipts.jsonl and run.minds.json."""
    from ..minds.driver import MindsRunConfig, run_minds
    from ..minds.pilot import PilotSkill

    governed = not ungoverned
    skill = PilotSkill(aim_lag=pilot_aim_lag, fire_on=pilot_fire_on, fire_off=pilot_fire_off)
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
        llm_mode=llm_mode,
    )
    if out is None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        out = base_dir / "minds" / f"{stamp}-{seed}"
    config = MindsRunConfig(
        seed=seed, ticks=ticks, cadence=cadence, hold=hold, governed=governed, scene=scene, pilot=skill, others_limit=others,
        nunchi=_nunchi(nunchi, nunchi_rpc), settle_dir=out, chain_budget=chain_budget, revoke_at_tick=revoke_at, revoke_mind=revoke_mind,
        quest=_load_quest(quest) if quest is not None else None,
    )
    if quest is not None:
        scene = f"quest:{quest.name}"
    condition = "scripted" if mind == "idle" else ("governed" if governed else "ungoverned")
    typer.echo(f"minds run: {condition} scene={scene} mind={mind} x{len(bound)} seed={seed} ticks<={ticks} cadence={cadence}", err=True)
    run = run_minds(bound, config, progress=_progress_printer(600))
    verified, mismatches = _verify(run)
    replay_path = _write_run(run, out, condition=condition, kind=mind, verified=verified, mismatches=mismatches)
    typer.echo(_summary_line(condition, run, verified=verified))
    if run.settlement:
        typer.echo(_settlement_line(run))
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
    llm_mode: str = typer.Option("moves", "--llm-mode", help="moves or coords"),
    scene: str = typer.Option("arena", "--scene", help="arena or survival"),
    others: int = typer.Option(12, "--others", help="nearest other creatures shown to a mind"),
    pilot_aim_lag: float = typer.Option(40.0, "--pilot-aim-lag"),
    pilot_fire_on: int = typer.Option(20, "--pilot-fire-on"),
    pilot_fire_off: int = typer.Option(20, "--pilot-fire-off"),
    conditions: str = typer.Option("scripted,ungoverned,governed", "--conditions", help="comma-separated subset"),
    out: Path | None = typer.Option(None, "--out", help="output directory (default: <base-dir>/minds/compare-<timestamp>)"),
    nunchi: bool = typer.Option(False, "--nunchi/--no-nunchi", help="admit and settle every run's minds on the Nunchi chain"),
    nunchi_rpc: str = typer.Option("http://127.0.0.1:47100", "--nunchi-rpc"),
    chain_budget: int = typer.Option(2000, "--chain-budget", help="credits issued on chain to an ungoverned mind"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Play scripted, ungoverned and governed conditions over the same seeds and print one table."""
    from ..minds.driver import MindsRunConfig, run_minds
    from ..minds.pilot import PilotSkill

    skill = PilotSkill(aim_lag=pilot_aim_lag, fire_on=pilot_fire_on, fire_off=pilot_fire_off)
    chain = _nunchi(nunchi, nunchi_rpc)
    wanted = tuple(part.strip() for part in conditions.split(",") if part.strip())
    if any(part not in ("scripted", "ungoverned", "governed") for part in wanted):
        typer.echo(f"unknown condition in {conditions!r}", err=True)
        raise typer.Exit(code=2)
    if out is None:
        out = base_dir / "minds" / f"compare-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    for offset in range(max(1, int(seeds))):
        run_seed = (int(seed) + offset) & 0xFFFFFFFF
        for condition in wanted:
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
                llm_mode=llm_mode,
            )
            typer.echo(f"  {condition:<10} seed={run_seed} ...", err=True)
            run_dir = out / f"{condition}-{run_seed}"
            run = run_minds(
                bound,
                MindsRunConfig(
                    seed=run_seed, ticks=ticks, cadence=cadence, governed=governed, scene=scene, pilot=skill, others_limit=others,
                    nunchi=chain, settle_dir=run_dir, chain_budget=chain_budget,
                ),
            )
            verified, mismatches = _verify(run)
            _write_run(run, run_dir, condition=condition, kind=kind, verified=verified, mismatches=mismatches)
            if run.settlement:
                typer.echo("    " + _settlement_line(run), err=True)
            accounts = run.accounts
            rows.append(
                {
                    "condition": condition,
                    "seed": run_seed,
                    "ticks": run.ticks,
                    "outcome": str(run.result.outcome),
                    "elapsed_ms": run.result.elapsed_ms,
                    "health": round(run.player_health),
                    "damage": round(run.damage_taken),
                    "kills": run.result.kills,
                    "xp": run.result.players[0].experience,
                    "steers": run.steers_recorded,
                    "inferences": sum(account.inferences for account in accounts),
                    "tokens": sum(account.tokens for account in accounts),
                    "spent": sum(account.spent for account in accounts),
                    "refused": sum(account.refusals for account in accounts),
                    "invalid": sum(account.invalid for account in accounts),
                    "verified": verified,
                    "chain": _chain_cell(run),
                    "wall_s": round(run.wall_s, 2),
                    "mind_s": round(run.mind_s, 2),
                },
            )
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    columns = ("condition", "seed", "ticks", "outcome", "elapsed_ms", "health", "damage", "kills", "xp", "steers", "inferences", "tokens", "spent", "refused", "invalid", "verified", "chain", "wall_s", "mind_s")
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in columns}
    typer.echo("  ".join(column.ljust(widths[column]) for column in columns))
    for row in rows:
        typer.echo("  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    typer.echo(f"wrote {out / 'compare.json'} and one directory per run ({time.perf_counter() - started:.1f}s)")


def _video_seconds(ffmpeg: str, path: Path) -> float:
    """A video's duration, from ffprobe next to `ffmpeg`."""
    import subprocess

    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def _label_png(text: str, path: Path, *, width: int) -> None:
    """A translucent label strip as a PNG, for ffmpeg builds without the drawtext filter."""

    from PIL import Image, ImageDraw, ImageFont

    height = 44
    image = Image.new("RGBA", (width, height), (0, 0, 0, 150))
    draw = ImageDraw.Draw(image)
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    draw.text((14, 9), text, fill=(255, 255, 255, 255), font=font)
    image.save(path)


@minds_app.command("grid")
def cmd_minds_grid(
    replays: list[Path] = typer.Argument(..., help="two or four replays (.crd): left/right, or a 2x2 grid in reading order"),
    out: Path = typer.Option(..., "--out", help="output .mp4"),
    labels: str = typer.Option("", "--labels", help="comma-separated labels, one per replay (default: the run's minds.json condition and mind)"),
    size: int = typer.Option(640, "--size", help="render size per tile (square)"),
    max_ticks: int | None = typer.Option(None, "--max-ticks"),
    assets_dir: Path = typer.Option(Path("artifacts/assets"), "--assets-dir", help="assets root holding the .paq files"),
    meters: bool = typer.Option(True, "--meters/--no-meters", help="metering strip + enemies tinted by mind (runs with run.minds.json)"),
    title: str = typer.Option("", "--title", help="title card text (3 s before the grid)"),
    subtitle: str = typer.Option("", "--subtitle", help="title card subtitle"),
    end_card: bool = typer.Option(True, "--end-card/--no-end-card", help="AI bill card after the grid (runs settled on Nunchi)"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Render two or four replays as one labelled video: side by side, or a 2x2 grid, through ffmpeg."""
    import shutil
    import subprocess
    import tempfile

    from ..minds.video import MeterTimeline, make_callbacks
    from ..render.world.overrides import clear_creature_tints
    from ..replay.codec import load_replay_file
    from ..replay.driver.replay_render import ReplayRenderError, run_replay_render_video

    if len(replays) not in (2, 4):
        typer.echo("give two or four replays", err=True)
        raise typer.Exit(code=2)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        typer.echo("ffmpeg not found on PATH", err=True)
        raise typer.Exit(code=1)
    names = [part.strip() for part in labels.split(",")] if labels else []
    if names and len(names) != len(replays):
        typer.echo("one label per replay", err=True)
        raise typer.Exit(code=2)
    if not names:
        for path in replays:
            meta = path.with_name("run.minds.json")
            if meta.is_file():
                data = json.loads(meta.read_text(encoding="utf-8"))
                names.append(f"{data.get('condition', '')} {data.get('mind', '')}".strip())
            else:
                names.append(path.stem)
    with tempfile.TemporaryDirectory(prefix="crimson-minds-grid-") as tmp:
        tiles: list[Path] = []
        for index, path in enumerate(replays):
            tile = Path(tmp) / f"tile-{index}.mp4"
            typer.echo(f"rendering {names[index]}: {path}", err=True)
            before_draw = on_frame = None
            if meters and path.with_name("run.minds.json").is_file() and path.with_name("receipts.jsonl").is_file():
                before_draw, on_frame = make_callbacks(MeterTimeline.from_run_dir(path.parent), width=size, height=size)
            try:
                run_replay_render_video(
                    load_replay_file(path),
                    replay_path=path,
                    output_path=tile,
                    base_dir=base_dir,
                    assets_dir=assets_dir,
                    width=size,
                    height=size,
                    max_ticks=max_ticks,
                    preset="veryfast",
                    overwrite=True,
                    before_draw=before_draw,
                    on_frame=on_frame,
                )
            except ReplayRenderError as exc:
                typer.echo(f"render failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            finally:
                clear_creature_tints()
            label = Path(tmp) / f"label-{index}.png"
            _label_png(names[index], label, width=size)
            tiles.append(tile)
        inputs: list[str] = []
        for index, tile in enumerate(tiles):
            inputs += ["-i", str(tile), "-i", str(Path(tmp) / f"label-{index}.png")]
        # A game that ends early holds its last frame until the longest one ends, so every ending stays visible.
        durations = [_video_seconds(ffmpeg, tile) for tile in tiles]
        longest = max(durations)
        graph = "".join(
            f"[{2 * index}:v]tpad=stop_mode=clone:stop_duration={longest - durations[index] + 0.1:.3f}[p{index}];"
            f"[p{index}][{2 * index + 1}:v]overlay=0:0[t{index}];"
            for index in range(len(tiles))
        )
        if len(tiles) == 2:
            graph += "[t0][t1]hstack=inputs=2[v]"
        else:
            graph += "[t0][t1][t2][t3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[v]"
        out.parent.mkdir(parents=True, exist_ok=True)
        grid = Path(tmp) / "grid.mp4"
        command = [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[v]", "-t", f"{longest:.3f}"]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "60", str(grid)]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            typer.echo(f"ffmpeg failed: {completed.stderr.strip()}", err=True)
            raise typer.Exit(code=1)
        grid_w, grid_h = (2 * size, size) if len(tiles) == 2 else (2 * size, 2 * size)
        parts: list[tuple[Path, float]] = []
        if title:
            from ..minds.cards import title_card

            card = Path(tmp) / "title.png"
            title_card(card, width=grid_w, height=grid_h, title=title, subtitle=subtitle)
            parts.append((card, 4.0))
        parts.append((grid, 0.0))
        settled = [path.parent for path in replays if path.with_name("run.minds.json").is_file()]
        if end_card and len(settled) == len(replays) and all(
            json.loads((d / "run.minds.json").read_text(encoding="utf-8")).get("settlement") for d in settled
        ):
            from ..minds.cards import end_card as draw_end_card

            blocks = list(zip(settled, names, strict=True))
            card = Path(tmp) / "end.png"
            draw_end_card(card, width=grid_w, height=grid_h, tiles=blocks)
            parts.append((card, 7.0))
        if len(parts) == 1:
            shutil.copyfile(grid, out)
        else:
            inputs = []
            for part, seconds in parts:
                inputs += ["-loop", "1", "-t", f"{seconds}", "-framerate", "60", "-i", str(part)] if seconds else ["-i", str(part)]
            scaled = "".join(f"[{i}:v]scale={grid_w}:{grid_h},setsar=1,fps=60,format=yuv420p[c{i}];" for i in range(len(parts)))
            joined = "".join(f"[c{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[v]"
            command = [ffmpeg, "-y", "-loglevel", "error", *inputs, "-filter_complex", scaled + joined, "-map", "[v]"]
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
            if completed.returncode != 0:
                typer.echo(f"ffmpeg failed: {completed.stderr.strip()}", err=True)
                raise typer.Exit(code=1)
    typer.echo(f"wrote {out}")


def _spread(values: list[float]) -> dict[str, float]:
    import statistics

    return {"min": round(min(values), 1), "mean": round(statistics.mean(values), 1), "max": round(max(values), 1)}


@minds_app.command("stochastic")
def cmd_minds_stochastic(
    mind: str = typer.Option("llm", "--mind", help="random, flank, surround or llm"),
    reps: int = typer.Option(5, "--reps", help="how many times to play the same seed"),
    minds: int = typer.Option(2, "--minds"),
    seed: int = typer.Option(7, "--seed", help="the one run seed every repetition plays"),
    ticks: int = typer.Option(2400, "--ticks"),
    cadence: int = typer.Option(30, "--cadence"),
    scene: str = typer.Option("ring", "--scene"),
    others: int = typer.Option(6, "--others", help="nearest other creatures shown to a mind"),
    ungoverned: bool = typer.Option(True, "--ungoverned/--governed"),
    budget: int = typer.Option(120, "--budget", help="credits per mind when governed"),
    price: int = typer.Option(10, "--price"),
    url: str = typer.Option("http://127.0.0.1:8083", "--url"),
    model: str = typer.Option("", "--model"),
    temperature: float = typer.Option(0.9, "--temperature"),
    llm_seed: int | None = typer.Option(None, "--llm-seed", help="fix the model's sampling seed for every repetition (a determinism check)"),
    mind_seed_step: int = typer.Option(1, "--mind-seed-step", help="model-free minds: seed offset between repetitions (0 = same seed every time)"),
    timeout_s: float = typer.Option(180.0, "--timeout"),
    pilot_aim_lag: float = typer.Option(40.0, "--pilot-aim-lag"),
    pilot_fire_on: int = typer.Option(20, "--pilot-fire-on"),
    pilot_fire_off: int = typer.Option(20, "--pilot-fire-off"),
    out: Path | None = typer.Option(None, "--out"),
    nunchi: bool = typer.Option(False, "--nunchi/--no-nunchi", help="admit and settle every game's minds on the Nunchi chain"),
    nunchi_rpc: str = typer.Option("http://127.0.0.1:47100", "--nunchi-rpc"),
    chain_budget: int = typer.Option(2000, "--chain-budget"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Play one seed many times with the same pilot and see how much the games differ, each still verifying.

    This is the stochastic-game test: the simulation is deterministic and the recording replays bit for bit, but
    a mind that samples (a model at temperature, a heuristic on a fresh seed) makes a different game each time.
    The table shows the spread; `distinct` counts recordings with different bytes.
    """
    import hashlib

    from ..minds.driver import MindsRunConfig, run_minds
    from ..minds.pilot import PilotSkill
    from ..replay.codec import encode_replay_payload

    skill = PilotSkill(aim_lag=pilot_aim_lag, fire_on=pilot_fire_on, fire_off=pilot_fire_off)
    if out is None:
        out = base_dir / "minds" / f"stochastic-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}"
    chain = _nunchi(nunchi, nunchi_rpc)
    rows: list[dict[str, object]] = []
    digests: list[str] = []
    alive_list: list[float] = []
    health_list: list[float] = []
    kills_list: list[float] = []
    started = time.perf_counter()
    for rep in range(max(1, int(reps))):
        bound = _build_minds(
            mind,
            count=minds,
            seed=seed + rep * mind_seed_step,
            budget=None if ungoverned else budget,
            price=price,
            url=url,
            model=model,
            temperature=temperature,
            llm_seed=llm_seed,
            timeout_s=timeout_s,
            llm_mode="moves",
        )
        typer.echo(f"  rep {rep + 1}/{reps} seed={seed} mind={mind} ...", err=True)
        config = MindsRunConfig(
            seed=seed, ticks=ticks, cadence=cadence, governed=not ungoverned, scene=scene, pilot=skill, others_limit=others,
            nunchi=chain, settle_dir=out / f"rep-{rep}", chain_budget=chain_budget,
        )
        run = run_minds(bound, config)
        verified, mismatches = _verify(run)
        digest = hashlib.sha256(encode_replay_payload(run.replay)).hexdigest()
        digests.append(digest)
        _write_run(run, out / f"rep-{rep}", condition="ungoverned" if ungoverned else "governed", kind=mind, verified=verified, mismatches=mismatches)
        accounts = run.accounts
        alive = run.end_tick if run.end_tick is not None else run.ticks
        alive_list.append(alive / 60)
        health_list.append(run.player_health)
        kills_list.append(float(run.result.kills))
        rows.append(
            {
                "rep": rep,
                "outcome": str(run.result.outcome),
                "alive_s": round(alive / 60, 1),
                "health": round(run.player_health),
                "kills": run.result.kills,
                "steers": run.steers_recorded,
                "inferences": sum(account.inferences for account in accounts),
                "failed": sum(account.failed_inferences for account in accounts),
                "tokens": sum(account.tokens for account in accounts),
                "refused": sum(account.refusals for account in accounts),
                "verified": verified,
                "recording": digest[:12],
                "chain": _chain_cell(run),
                "wall_s": round(run.wall_s, 1),
            },
        )
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "mind": mind,
        "seed": seed,
        "reps": len(rows),
        "temperature": temperature,
        "llm_seed": llm_seed,
        "distinct_recordings": len(set(digests)),
        "all_verified": all(bool(row["verified"]) for row in rows),
        "alive_s": _spread(alive_list),
        "health": _spread(health_list),
        "kills": _spread(kills_list),
        "deaths": sum(row["outcome"] == "death" for row in rows),
    }
    (out / "stochastic.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    columns = ("rep", "outcome", "alive_s", "health", "kills", "steers", "inferences", "failed", "tokens", "refused", "verified", "recording", "chain", "wall_s")
    widths = {column: max(len(column), *(len(str(row[column])) for row in rows)) for column in columns}
    typer.echo("  ".join(column.ljust(widths[column]) for column in columns))
    for row in rows:
        typer.echo("  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    typer.echo(
        f"{summary['reps']} games of seed {seed}: {summary['distinct_recordings']} distinct recordings, "
        f"{summary['deaths']} pilot deaths, alive {summary['alive_s']}, health {summary['health']}, kills {summary['kills']}, "
        f"all verified={summary['all_verified']} ({time.perf_counter() - started:.0f}s)",
    )
    typer.echo(f"wrote {out / 'stochastic.json'}")


@minds_app.command("audit")
def cmd_minds_audit(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, help="a run directory written with --nunchi"),
    nunchi_rpc: str | None = typer.Option(None, "--nunchi-rpc", help="chain RPC (default: the one the run settled on)"),
) -> None:
    """Re-check a settled run from its files and the chain, trusting neither the run's summary nor the game.

    Replays the recording; recomputes the recording's and the receipt log's SHA-256 and the settlement receipt;
    re-adds each mind's spend from the receipt log; and reads each mind's lease authorization, balance and usage
    back from the chain.
    """
    from ..minds.audit import audit_run

    try:
        checks = audit_run(run_dir, rpc=nunchi_rpc)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    width = max(len(name) for name, _, _ in checks)
    for name, passed, note in checks:
        typer.echo(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {note}")
    failed = sum(not passed for _, passed, _ in checks)
    typer.echo(f"{len(checks) - failed}/{len(checks)} checks passed")
    if failed:
        raise typer.Exit(code=1)


@minds_app.command("clip")
def cmd_minds_clip(
    run_dirs: list[Path] = typer.Argument(..., help="two or four run directories (each with run.crd, receipts.jsonl, run.minds.json)"),
    out: Path = typer.Option(..., "--out", help="output .mp4"),
    names: str = typer.Option(..., "--names", help="panel names separated by ';', one per run"),
    title: str = typer.Option("Meter AI usage. Set a budget.", "--title"),
    corner: str = typer.Option("Recorded demo", "--corner", help="small persistent label, top right"),
    legend: str = typer.Option("Ringed enemies are steered by the AI model; the rest run on the game's built-in AI.", "--legend", help="four-panel clips only"),
    seconds: float = typer.Option(22.0, "--seconds", help="clip length; one game tick per frame at 60 fps"),
    tile: int = typer.Option(512, "--tile", help="game panel size in pixels"),
    assets_dir: Path = typer.Option(Path("artifacts/assets"), "--assets-dir"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """A readable metering clip: the games with their AI-steered enemies ringed, large meters under each game."""
    import shutil
    import tempfile

    from ..minds.compose import Panel, compose, compose_story
    from ..minds.video import MeterTimeline, make_callbacks
    from ..render.world.overrides import clear_creature_tints
    from ..replay.codec import load_replay_file
    from ..replay.driver.replay_render import ReplayRenderError, run_replay_render_video

    labels = [part.strip() for part in names.split(";")]
    if len(run_dirs) not in (2, 4) or len(labels) != len(run_dirs):
        typer.echo("give two or four run directories and one name per run", err=True)
        raise typer.Exit(code=2)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        typer.echo("ffmpeg not found on PATH", err=True)
        raise typer.Exit(code=1)
    frames = int(round(seconds * 60))
    with tempfile.TemporaryDirectory(prefix="crimson-minds-clip-") as tmp:
        panels: list[Panel] = []
        for index, (run_dir, label) in enumerate(zip(run_dirs, labels, strict=True)):
            timeline = MeterTimeline.from_run_dir(run_dir)
            replay = load_replay_file(run_dir / "run.crd")
            video = Path(tmp) / f"panel-{index}.mp4"
            rings: list[list[tuple[int, int, int, int]]] = []
            before_draw, on_frame = make_callbacks(timeline, width=tile, height=tile, strip=False, ring_log=rings)
            typer.echo(f"rendering {label}: {run_dir}", err=True)
            try:
                run_replay_render_video(
                    replay, replay_path=run_dir / "run.crd", output_path=video, base_dir=base_dir, assets_dir=assets_dir,
                    width=tile, height=tile, max_ticks=frames, preset="veryfast", overwrite=True,
                    before_draw=before_draw, on_frame=on_frame,
                )
            except ReplayRenderError as exc:
                typer.echo(f"render failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            finally:
                clear_creature_tints()
            enforced = all(track.governed for track in timeline.tracks)
            panels.append(Panel(video=video, timeline=timeline, name=label, enforced=enforced, ticks=len(replay.ticks), rings=rings))
        out.parent.mkdir(parents=True, exist_ok=True)
        typer.echo(f"composing {frames} frames", err=True)
        if len(panels) == 2:
            # Two panels are the application comparison: timed captions replace the static legend.
            moments = compose_story(panels, out, ffmpeg=ffmpeg, title=title, corner=corner, frames=frames, tile=tile)
            typer.echo(" · ".join(f"{key} {value}" for key, value in moments.items()), err=True)
        else:
            compose(panels, out, ffmpeg=ffmpeg, title=title, corner=corner, legend=legend, frames=frames, tile=tile)
    typer.echo(f"wrote {out}")


@minds_app.command("build")
def cmd_minds_build(
    builder: str = typer.Option("jev", "--builder", help="jev (picks waves from the catalog), llm (writes the recipe) or scripted (no model)"),
    attempts: int = typer.Option(20, "--attempts", help="proposals at most"),
    target: int = typer.Option(20, "--target", help="stop once this many quests are accepted"),
    budget: int = typer.Option(30, "--budget", help="credits the builder is issued (held to it unless --ungoverned)"),
    price: int = typer.Option(1, "--price", help="credits per 1k tokens"),
    ungoverned: bool = typer.Option(False, "--ungoverned", help="meter but enforce nothing: no budget cap, policy violations still run"),
    seed: int = typer.Option(1, "--seed", help="the builder's sampling seed"),
    temperature: float = typer.Option(1.0, "--temperature"),
    url: str = typer.Option("http://127.0.0.1:8083", "--url", help="chat-completions server for --builder llm"),
    model: str = typer.Option("", "--model", help="model name for --builder llm"),
    think: bool = typer.Option(False, "--think/--no-think", help="let a thinking model reason before it writes (slow)"),
    max_creatures: int = typer.Option(64, "--max-creatures", help="policy: creatures in the whole quest"),
    max_seconds: int = typer.Option(30, "--max-seconds", help="policy: the last wave's time"),
    goal: str | None = typer.Option(None, "--goal", help="the design goal in the builder's brief (default: clearable after a real fight)"),
    out: Path | None = typer.Option(None, "--out", help="output directory (default: <base-dir>/minds/build-<timestamp>)"),
    nunchi: bool = typer.Option(False, "--nunchi/--no-nunchi", help="admit and settle the builder on the Nunchi chain"),
    nunchi_rpc: str = typer.Option("http://127.0.0.1:47100", "--nunchi-rpc"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """A builder proposes quests under a lease: policy-checked before they run, playtested, accepted with a proof replay."""
    from ..minds.build import BuildConfig, BuildPolicy, JevBuilder, LlmBuilder, ScriptedBuilder, run_build, write_build

    policy = BuildPolicy(max_creatures=max_creatures, max_seconds=max_seconds)
    usd_per_mtok = 0.0
    match builder:
        case "jev":
            chosen = JevBuilder(seed=seed, temperature=temperature)
            usd_per_mtok = JEV_USD_PER_MTOK
        case "llm":
            chosen = LlmBuilder(url=url, model=model, temperature=temperature, seed=seed, think=think)
        case "scripted":
            chosen = ScriptedBuilder(policy, seed=seed)
        case _:
            typer.echo(f"unknown builder {builder!r}; use jev, llm or scripted", err=True)
            raise typer.Exit(code=2)
    if out is None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        out = base_dir / "minds" / f"build-{stamp}"
    config = BuildConfig(
        attempts=attempts, target=target, governed=not ungoverned, budget=budget, price_per_1k_tokens=price,
        usd_per_mtok=usd_per_mtok, policy=policy, nunchi=_nunchi(nunchi, nunchi_rpc),
        **({"goal": goal} if goal else {}),
    )
    condition = "ungoverned" if ungoverned else "governed"
    typer.echo(f"minds build: {condition} builder={builder} attempts<={attempts} budget={budget} cr", err=True)
    run = run_build(chosen, config, out, progress=lambda line: typer.echo(line, err=True))
    write_build(run, out)
    summary = run.summary()
    account = run.account
    typer.echo(
        f"{condition} {builder}: {summary['accepted']} accepted of {summary['attempts']} attempts {summary['verdicts']} · "
        f"{account.inferences} calls · {account.tokens:,} tokens · {account.spent} credits"
        + (f" / {budget}" if not ungoverned else f" (allowance {budget})")
        + f" · policy violations proposed {summary['violations_proposed']}, ran {summary['violations_executed']}",
    )
    if run.settlement:
        settled = run.settlement["settlement"]
        typer.echo(f"chain: {json.dumps({key: settled[key] for key in settled if key in ('debited', 'refused', 'balance_after', 'txs')})}")
    typer.echo(f"wrote {out} (attempts.jsonl, receipts.jsonl, accepted.json, quests/, playtests/)")


@minds_app.command("build-report")
def cmd_minds_build_report(
    build_dirs: list[Path] = typer.Argument(..., help="build directories (from `crimson minds build`)"),
    out: Path | None = typer.Option(None, "--out", help="also write the table as markdown here"),
) -> None:
    """The Nunchi Worlds prototype metrics across builders: pass rate, policy, budget, audit, control overhead."""
    from ..minds.build import report_rows

    rows = report_rows(build_dirs)
    header = ["Builder", "Attempts", "Accepted", "Pass rate (run)", "Violations proposed", "Violations ran", "Accepted in breach", "Credits / allowance", "Tokens", "Audit", "Control overhead"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(row[key]) for key in header) + " |" for row in rows]
    table = "\n".join(lines)
    typer.echo(table)
    if out is not None:
        out.write_text(table + "\n", encoding="utf-8")


@minds_app.command("build-clip")
def cmd_minds_build_clip(
    left_dir: Path = typer.Argument(..., help="the enforced build directory"),
    right_dir: Path = typer.Argument(..., help="the unenforced build directory (same builder, brief and seed)"),
    out: Path = typer.Option(..., "--out", help="output .mp4"),
    title: str = typer.Option("An AI builds levels. The lease sets the rules.", "--title"),
    corner: str = typer.Option("Recorded runs · sped up", "--corner"),
    names: str = typer.Option("Lease enforced;Lease not enforced", "--names", help="the two panel names, separated by ';'"),
    tile: int = typer.Option(512, "--tile"),
    assets_dir: Path = typer.Option(Path("artifacts/assets"), "--assets-dir"),
    base_dir: Path = _BASE_DIR,
) -> None:
    """Two builders side by side: the first proposal at length, then a ticker to the left's budget cap."""
    import shutil
    import tempfile

    from ..minds.build_clip import Builds, compose_build_story, schedule
    from ..replay.codec import load_replay_file
    from ..replay.driver.replay_render import ReplayRenderError, run_replay_render_video

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        typer.echo("ffmpeg not found on PATH", err=True)
        raise typer.Exit(code=1)
    labels = tuple(part.strip() for part in names.split(";"))
    if len(labels) != 2:
        typer.echo("give two panel names separated by ';'", err=True)
        raise typer.Exit(code=2)
    builds = [Builds.load(left_dir), Builds.load(right_dir)]
    with tempfile.TemporaryDirectory(prefix="crimson-build-clip-") as tmp:
        for side, build in enumerate(builds):
            # Render only the playtests the clip will show, up to the end of their excerpt.
            for slot in schedule(build, intro_frames=480, step_frames=75, total=10**6)[:16]:
                attempt = slot.attempt
                if attempt is None or not attempt.playtests:
                    continue
                proof = attempt.playtests[0]
                replay_path = build.path / "playtests" / proof.replay_file
                video = Path(tmp) / f"side{side}-attempt{attempt.attempt + 1:02d}.mp4"
                typer.echo(f"rendering {replay_path.name} ({'left' if side == 0 else 'right'})", err=True)
                try:
                    run_replay_render_video(
                        load_replay_file(replay_path), replay_path=replay_path, output_path=video, base_dir=base_dir,
                        assets_dir=assets_dir, width=tile, height=tile, max_ticks=slot.excerpt_from + slot.frames + 10,
                        preset="veryfast", overwrite=True,
                    )
                except ReplayRenderError as exc:
                    typer.echo(f"render failed: {exc}", err=True)
                    raise typer.Exit(code=1) from exc
                build.excerpts[attempt.attempt] = video
        out.parent.mkdir(parents=True, exist_ok=True)
        moments = compose_build_story(builds[0], builds[1], out, ffmpeg=ffmpeg, title=title, corner=corner, names=labels, tile=tile)
    typer.echo(" · ".join(f"{key} {value}" for key, value in moments.items()), err=True)
    typer.echo(f"wrote {out}")
