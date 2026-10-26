"""Credit accounts for minds: inference is charged on reported tokens, steers are authorized or refused.

This is the control plane in miniature. A governed ledger holds each mind to its budget and to the
creatures it owns; an ungoverned one meters the same things and refuses nothing it can legally record.
Either way a steer the simulation would reject (a dead slot, a point off the arena) is dropped as
invalid before it reaches the recording. Every event is a `Receipt`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

import msgspec

from .minds import Steer


class Receipt(msgspec.Struct, frozen=True):
    tick: int
    mind_id: int
    # "inference": a charged decision; "refusal": a steer or inference the policy refused;
    # "invalid": a steer the simulation would not accept; "steer": an accepted steer.
    kind: str
    tokens: int = 0
    cost: int = 0
    balance: int = 0
    detail: str = ""


class Account(msgspec.Struct):
    mind_id: int
    name: str
    model: str
    # None means unlimited (the ungoverned condition).
    budget: int | None
    balance: int
    price_per_1k_tokens: int
    inferences: int = 0
    failed_inferences: int = 0
    tokens: int = 0
    spent: int = 0
    steers: int = 0
    refusals: int = 0
    invalid: int = 0
    # The dearest decision so far: a governed mind is not asked when its balance could not cover another one.
    max_cost: int = 0


def inference_cost(tokens: int, price_per_1k_tokens: int) -> int:
    """`ceil(tokens * price / 1000)` credits, the per-token price applied to the reported usage."""

    return int(math.ceil(int(tokens) * int(price_per_1k_tokens) / 1000.0))


class Ledger:
    def __init__(self, accounts: Iterable[Account], *, governed: bool) -> None:
        self.accounts: dict[int, Account] = {int(account.mind_id): account for account in accounts}
        self.governed = bool(governed)
        self.receipts: list[Receipt] = []
        # Minds whose permission the chain has revoked (governed runs stop asking them).
        self.revoked: set[int] = set()

    def revoke(self, mind_id: int, tick: int, *, detail: str = "revoked_on_chain") -> None:
        """Record that the chain no longer permits this mind; a governed ledger refuses it from now on."""

        account = self.accounts[int(mind_id)]
        if int(mind_id) not in self.revoked:
            self.revoked.add(int(mind_id))
            self._receipt(account, tick, "revocation", detail=detail)

    def _receipt(self, account: Account, tick: int, kind: str, *, tokens: int = 0, cost: int = 0, detail: str = "") -> None:
        self.receipts.append(
            Receipt(tick=int(tick), mind_id=account.mind_id, kind=kind, tokens=tokens, cost=cost, balance=account.balance, detail=detail),
        )

    def may_infer(self, mind_id: int, tick: int) -> bool:
        """Whether the mind may be asked at all: a governed account with nothing left is refused before the call."""

        account = self.accounts[int(mind_id)]
        if self.governed and int(mind_id) in self.revoked:
            account.refusals += 1
            self._receipt(account, tick, "refusal", detail="revoked_on_chain")
            return False
        if self.governed and account.budget is not None and (account.balance <= 0 or account.balance < account.max_cost):
            # Charged after the call, so the check reserves the dearest call seen: the balance never goes negative
            # and the debit settled on chain never exceeds what the chain issued.
            account.refusals += 1
            self._receipt(account, tick, "refusal", detail="budget_exhausted")
            return False
        return True

    def charge_inference(self, mind_id: int, tick: int, *, prompt_tokens: int, completion_tokens: int, ok: bool) -> int:
        """Charge one decision on its reported tokens; a failed call is counted and costs nothing."""

        account = self.accounts[int(mind_id)]
        if not ok:
            account.failed_inferences += 1
            self._receipt(account, tick, "inference", detail="failed")
            return 0
        tokens = int(prompt_tokens) + int(completion_tokens)
        cost = inference_cost(tokens, account.price_per_1k_tokens)
        account.inferences += 1
        account.tokens += tokens
        account.spent += cost
        account.max_cost = max(account.max_cost, cost)
        if account.budget is not None:
            account.balance -= cost
        self._receipt(account, tick, "inference", tokens=tokens, cost=cost)
        return cost

    def authorize_steer(
        self,
        mind_id: int,
        tick: int,
        steer: Steer,
        *,
        owned: Callable[[int], bool],
        live: Callable[[int], bool],
        arena: float,
    ) -> str | None:
        """None when the steer may be recorded, else why not (a receipt is written either way)."""

        account = self.accounts[int(mind_id)]
        index = int(steer.creature_index)
        if not live(index):
            account.invalid += 1
            self._receipt(account, tick, "invalid", detail=f"slot {index} holds no live creature")
            return "dead_slot"
        if not (0.0 <= steer.target_x <= arena and 0.0 <= steer.target_y <= arena):
            account.invalid += 1
            self._receipt(account, tick, "invalid", detail=f"target off the arena for slot {index}")
            return "out_of_arena"
        if self.governed and not owned(index):
            account.refusals += 1
            self._receipt(account, tick, "refusal", detail=f"slot {index} is not owned by mind {account.mind_id}")
            return "not_owned"
        if self.governed and account.budget is not None and account.balance <= 0:
            account.refusals += 1
            self._receipt(account, tick, "refusal", detail="budget_exhausted")
            return "budget_exhausted"
        account.steers += 1
        self._receipt(account, tick, "steer", detail=f"slot {index} -> ({steer.target_x:.1f}, {steer.target_y:.1f})")
        return None

    def summary(self) -> list[dict[str, object]]:
        return [msgspec.structs.asdict(account) for account in self.accounts.values()]

    def receipts_jsonl(self) -> bytes:
        return b"".join(msgspec.json.encode(receipt) + b"\n" for receipt in self.receipts)
