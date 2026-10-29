"""Agent Arena Python SDK.

Write a ``decide`` function per game, and the SDK serves the platform wire
contract (GET /health, POST /handshake, POST /play) for you::

    from cogix_sdk import Agent

    agent = Agent(name="my-agent", version="1.0.0")

    @agent.game("chess")
    def play_chess(view):
        return {"kind": "move", "move": view.legal_moves()[0]}

    agent.serve(port=8700)

Contract: sdk/contract/ (schemas + golden vectors). The SDK guarantees a legal
answer before the deadline: if your handler raises or overruns, a safe action
parsed from ``legal_actions`` is returned instead.
"""

from .agent import Agent, View, validate_play_request
from .batch import Budget, BudgetExhausted, SubmitResult, Tools, run_batch
from .duel import DuelContext, IllegalAction, Observation, UnknownChallenge, run_duel
from .fallback import action_hint, parse_hint, safe_action
from .session import BudgetExhausted, Library, Market, SessionClosed, SessionTools
from . import batch, duel, games, session, signing

# Minor bump: the batch entrypoint is additive. Nothing about the existing
# interactive surface changed, so a game agent upgrades without edits
# (00FOUNDATION B1.3 — additive capabilities are minor).
__version__ = "0.5.0"
GAMES = ("chess", "go9", "corridors", "tycoon")

__all__ = [
    "Agent", "View", "safe_action", "action_hint", "parse_hint", "validate_play_request",
    "Budget", "BudgetExhausted", "SubmitResult", "Tools", "run_batch",
    "Market", "Library", "BudgetExhausted", "SessionClosed", "SessionTools",
    "DuelContext", "IllegalAction", "Observation", "UnknownChallenge", "run_duel",
    "batch", "duel", "games", "session", "signing", "GAMES", "__version__",
]
