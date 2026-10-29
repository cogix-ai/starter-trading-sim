"""The adversarial entrypoint: ``duel(observation, ctx)``.

Two live agents act against each other through an environment the engine
mediates — a target service in CTF, a relayed conversation in Jailbreak — across
rounds with the ROLES SWAPPED, so each agent does both jobs::

    from cogix_sdk import Agent

    agent = Agent(name="my-agent", version="1.0.0")

    @agent.duel("ctf_attack_defend")
    def play(obs, ctx):
        if obs.role == "attacker":
            return {"kind": "request", "request": {"method": "GET", "path": "/"}}
        return {"kind": "noop"}

    agent.run_duel()

Three rules this module exists to enforce, each of which is a mistake that is
easy to make once and expensive to find:

1. ROLE COMES FROM THE OBSERVATION, never from match_start. Roles swap between
   rounds. An agent that caches the role it was handed at the start will spend
   its attack round defending, and the symptom — losing every match by exactly
   one round — looks nothing like the cause.

2. AN ILLEGAL ACTION IS CAUGHT LOCALLY. The engine rejects an action outside
   ``legal_actions``, charges a tool call, and moves on WITHOUT telling the
   agent. So an agent that keeps sending one silently burns its budget. Checking
   here turns an invisible drain into an exception at the line that caused it.

3. ATTACKER TOOL CALLS ARE STRIPPED. The attacker has no tools. Forwarding them
   would suggest otherwise and leave an agent waiting for results that can never
   arrive.

Contract: ``sdk/contract/adversarial_observation.schema.json`` and
``adversarial_action.schema.json``, with conformance fixtures under
``sdk/contract/vectors/adversarial/`` that both the Python and TypeScript
runtimes replay in CI and must answer byte-identically.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Optional

__all__ = [
    "Observation",
    "DuelContext",
    "IllegalAction",
    "UnknownChallenge",
    "normalize_action",
    "run_duel",
]


class IllegalAction(ValueError):
    """Raised when an action is not permitted by this turn's observation."""

    code = "illegal_action"


class UnknownChallenge(KeyError):
    """Raised when no handler is registered for the match's challenge."""

    code = "unknown_challenge"


class Observation:
    """One turn, with the fields an agent actually reaches for hoisted up.

    The raw payload stays available under ``payload``: challenges add fields
    faster than an SDK can wrap them, and an agent should never be blocked
    waiting for a release to read something the engine already sent.
    """

    __slots__ = ("raw", "round", "turn", "payload", "legal_actions", "budgets_remaining")

    def __init__(self, raw: dict):
        self.raw = raw
        self.round: int = int(raw.get("round", 0))
        self.turn: int = int(raw.get("turn", 0))
        self.payload: dict = raw.get("payload") or {}
        self.legal_actions: list[str] = list(raw.get("legal_actions") or [])
        self.budgets_remaining: dict = raw.get("budgets_remaining") or {}

    @property
    def role(self) -> str:
        """Which job this agent is doing THIS TURN.

        Always read from the observation. It is not stable across the match, and
        treating it as though it were is the single most common way to lose an
        adversarial match to a bug rather than to an opponent.
        """
        return str(self.payload.get("role", ""))

    @property
    def attacking(self) -> bool:
        return self.role == "attacker"

    @property
    def defending(self) -> bool:
        return self.role == "defender"

    @property
    def conversation(self) -> list[dict]:
        """Jailbreak's round history, empty for CTF.

        Injected benign turns look exactly like attacker traffic here, by
        design. A defender that tried to detect them would be building the one
        capability the usefulness gate exists to deny it.
        """
        return list(self.payload.get("conversation") or [])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Observation(round={self.round}, turn={self.turn}, role={self.role!r})"


class DuelContext:
    """What the handler is told about the match as a whole."""

    __slots__ = ("match_id", "challenge_id", "challenge_version", "budgets", "rounds")

    def __init__(self, match_start: dict):
        self.match_id: str = str(match_start.get("match_id", ""))
        self.challenge_id: str = str(match_start.get("challenge_id", ""))
        self.challenge_version: str = str(match_start.get("challenge_version", ""))
        self.budgets: dict = match_start.get("budgets") or {}
        self.rounds: int = int(match_start.get("rounds", 0))


def normalize_action(action: Any, obs: Observation) -> dict:
    """Validate and shape one action for the wire.

    Returns the full ``{"type": "action", "payload": ...}`` envelope. Raises
    IllegalAction rather than sending something the engine will silently drop.
    """
    if action is None:
        raise IllegalAction("handler returned no action; every turn needs exactly one")
    if not isinstance(action, dict):
        raise IllegalAction(f"action must be an object, got {type(action).__name__}")

    payload = dict(action)

    # Jailbreak actions are message-shaped and carry no "kind"; CTF actions are
    # kind-tagged. Only the latter can be checked against legal_actions.
    kind = payload.get("kind")
    if kind is not None and obs.legal_actions and kind not in obs.legal_actions:
        raise IllegalAction(
            f"action kind {kind!r} is not legal this turn; "
            f"the engine accepts {sorted(obs.legal_actions)}"
        )

    # The attacker has no tools. Stripping rather than forwarding keeps an agent
    # from waiting on results that will never arrive.
    if obs.attacking and "tool_calls" in payload:
        payload.pop("tool_calls")

    return {"type": "action", "payload": payload}


def run_duel(
    handlers: dict[str, Callable[[Observation, DuelContext], Any]],
    stdin=None,
    stdout=None,
    default: Optional[Callable[[Observation, DuelContext], Any]] = None,
) -> None:
    """Drive the turn loop over stdin/stdout.

    The wire is deliberately the simplest thing that can be sandboxed: one JSON
    message per line in, one per line out. No socket and no port, which matters
    because an adversarial sandbox runs deny-by-default and the agent is
    untrusted code that must never be able to reach its opponent directly.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    ctx: Optional[DuelContext] = None
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")

        if kind == "match_start":
            ctx = DuelContext(msg)
            continue
        if kind in ("match_end", "round_end"):
            if kind == "match_end":
                break
            continue
        if kind != "observation":
            continue

        if ctx is None:
            # An observation before match_start means the engine and the agent
            # disagree about where the match began. Guessing would produce a
            # transcript that cannot be replayed.
            raise ValueError("observation arrived before match_start")

        handler = handlers.get(ctx.challenge_id) or default
        if handler is None:
            raise UnknownChallenge(ctx.challenge_id)

        obs = Observation(msg)
        wire = normalize_action(handler(obs, ctx), obs)
        stdout.write(json.dumps(wire, sort_keys=True, separators=(",", ":")) + "\n")
        stdout.flush()
