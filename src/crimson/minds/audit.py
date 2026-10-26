"""Re-check a settled minds run from its files and the chain (the `crimson minds audit` checks)."""

from __future__ import annotations

import json
from pathlib import Path

from ..replay.codec import encode_replay_payload, load_replay_file
from ..replay.driver.playback_driver import PlaybackDriver
from ..sim.run_result import run_result_mismatches
from .nunchi import NunchiChain, NunchiError, settlement_receipt, sha256_hex

type Check = tuple[str, bool, str]


def audit_run(run_dir: Path, *, rpc: str | None = None) -> list[Check]:
    """The audit's checks for a run directory written with `--nunchi`; raises ValueError for an unsettled run."""

    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.minds.json").read_text(encoding="utf-8"))
    settlement = meta.get("settlement")
    if not settlement:
        raise ValueError("this run was not settled on Nunchi (no settlement in run.minds.json)")
    chain = NunchiChain(rpc=rpc or settlement["chain_rpc"])
    checks: list[Check] = []

    replay = load_replay_file(run_dir / "run.crd")
    mismatches = run_result_mismatches(replay.result, PlaybackDriver(replay, warn_on_version_mismatch=False).run())
    checks.append(("the recording replays to the result it recorded", not mismatches, ",".join(mismatches)))
    replay_sha = sha256_hex(encode_replay_payload(replay))
    checks.append(("recording SHA-256 matches the settlement", replay_sha == settlement["replay_sha256"], replay_sha[:16]))
    receipts_bytes = (run_dir / "receipts.jsonl").read_bytes()
    receipts_sha = sha256_hex(receipts_bytes)
    checks.append(("receipt log SHA-256 matches the settlement", receipts_sha == settlement["receipts_sha256"], receipts_sha[:16]))
    log = [json.loads(line) for line in receipts_bytes.decode("utf-8").splitlines() if line.strip()]

    for entry in settlement["minds"]:
        mind_id = int(entry["mind"])
        principal = entry["principal"]
        admission = entry["admission"]
        settled = entry["settlement"]
        model = admission["model"]
        label = f"mind {mind_id} ({principal})"
        lease_path = run_dir / admission["lease_file"]
        checks.append((f"{label}: lease file is the one admitted", sha256_hex(lease_path.read_bytes()) == admission["lease_sha256"], admission["lease_file"]))
        receipt = settlement_receipt(replay_sha256=replay_sha, receipts_sha256=receipts_sha, principal=principal)
        checks.append((
            f"{label}: settlement receipt recomputes",
            receipt == settled["receipt"] and sha256_hex(receipt.encode()) == settled["receipt_digest"],
            settled["receipt_digest"][:16],
        ))
        spent = sum(int(row.get("cost", 0)) for row in log if row.get("mind_id") == mind_id and row.get("kind") == "inference")
        checks.append((f"{label}: receipt log adds up to the spend settled", spent == int(settled["spent"]), f"{spent} credits"))
        try:
            status = chain.status(principal=principal, model=model, lease=lease_path)
        except NunchiError as exc:
            checks.append((f"{label}: chain reachable", False, str(exc)))
            continue
        checks.append((f"{label}: chain holds the lease as authorized", bool(status["authorized"]), ""))
        checks.append((f"{label}: chain balance equals the settled balance", int(status["balance"]) == int(settled["balance_after"]), f"{status['balance']}"))
        checks.append((
            f"{label}: chain usage equals the settled usage",
            int(status["usage_cost_usd_micros_total"]) == int(settled["usage_cost_usd_micros_total"]) and int(status["usage_count"]) >= 1,
            f"{status['usage_cost_usd_micros_total']} micro-USD, {status['usage_count']} record(s)",
        ))
        if settled["settled"]:
            checks.append((f"{label}: chain debited exactly the spend", int(settled["balance_before"]) - int(settled["spent"]) == int(status["balance"]), ""))
        else:
            checks.append((
                f"{label}: chain refused the overdraw and left the balance whole",
                int(settled["balance_before"]) == int(status["balance"]),
                settled["refusal"],
            ))
    return checks
