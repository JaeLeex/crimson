"""Settle minds on a Nunchi chain: admission before the game, settlement bound to the recording after it.

The chain side is the `nunchi-minds` binary (repo `nunchi-minds`): a four-validator Commonware chain on the Nunchi
SDK's settlement module. This module drives it as a subprocess that prints one JSON object per command.

- **Admit** (before play): the chain grants the mind permission to use its model, issues its credit budget, and
  records the lease (who, which model, how much, which run) as an authorization keyed by the lease file's SHA-256.
  The game's ledger then enforces the budget the chain holds.
- **Check** (during play, every few seconds of game time): a read of the mind's permission; a revoked mind is
  refused further inference and its creatures fall back to the native AI.
- **Settle** (after play): the credits spent are debited on chain (the chain will not debit past the balance),
  and a usage record commits to a receipt naming the recording's and the receipt log's SHA-256, so the bill and
  the replay are bound together.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RPC = "http://127.0.0.1:47100"


class NunchiError(RuntimeError):
    pass


def default_binary() -> Path:
    configured = os.environ.get("NUNCHI_MINDS_BIN")
    if configured:
        return Path(configured)
    return Path.home() / "nunchi-minds" / "target" / "release" / "nunchi-minds"


@dataclass
class NunchiChain:
    rpc: str = DEFAULT_RPC
    binary: Path | None = None
    timeout_s: float = 180.0

    def _run(self, *args: str) -> dict[str, Any]:
        binary = self.binary or default_binary()
        if not binary.is_file():
            raise NunchiError(f"nunchi-minds binary not found at {binary} (set NUNCHI_MINDS_BIN)")
        completed = subprocess.run(
            [str(binary), *args, "--rpc", self.rpc], check=False, capture_output=True, text=True, timeout=self.timeout_s,
        )
        line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NunchiError(f"nunchi-minds {args[0]}: no JSON reply ({completed.stderr.strip()[-300:]})") from exc
        if not data.get("ok", False):
            raise NunchiError(f"nunchi-minds {args[0]}: {data.get('error', data)}")
        return data

    def admit(self, *, principal: str, model: str, budget: int, lease: Path) -> dict[str, Any]:
        return self._run("admit", "--principal", principal, "--model", model, "--budget", str(int(budget)), "--lease", str(lease))

    def settle(self, *, principal: str, model: str, spent: int, cost_usd_micros: int, receipt: str) -> dict[str, Any]:
        return self._run(
            "settle", "--principal", principal, "--model", model, "--spent", str(int(spent)),
            "--cost-usd-micros", str(int(cost_usd_micros)), "--receipt", receipt,
        )

    def status(self, *, principal: str, model: str, lease: Path | None = None) -> dict[str, Any]:
        args = ["status", "--principal", principal, "--model", model]
        if lease is not None:
            args += ["--lease", str(lease)]
        return self._run(*args)

    def permitted(self, *, principal: str, model: str) -> bool:
        return bool(self.status(principal=principal, model=model)["permitted"])

    def revoke(self, *, principal: str, model: str) -> dict[str, Any]:
        return self._run("revoke", "--principal", principal, "--model", model)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def settlement_receipt(*, replay_sha256: str, receipts_sha256: str, principal: str) -> str:
    """The string a usage record commits to (the chain stores its SHA-256): the recording, the receipt log, the mind."""

    return f"crimson-minds/v1 replay={replay_sha256} receipts={receipts_sha256} principal={principal}"


def write_lease(path: Path, lease: dict[str, Any]) -> bytes:
    """Write a lease as canonical JSON (sorted keys, no spaces) and return the bytes the chain will hash."""

    data = json.dumps(lease, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data
