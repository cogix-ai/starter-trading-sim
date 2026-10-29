"""Local selftest: replay golden play_request fixtures through the agent.

The fixtures in sdk/contract/vectors/play/<game>.json were captured from the
real Go engines, so this validates your handler against exactly the state
shapes and legal_actions the platform will send — without any network or
platform account. It is a pre-flight check, not the certification itself:
final certification always runs on the platform's engines.
"""

from __future__ import annotations

import json
import pathlib
from typing import Optional

from .fallback import action_hint, safe_action


class SelftestFailure(AssertionError):
    pass


def _default_fixtures_dir() -> Optional[pathlib.Path]:
    here = pathlib.Path(__file__).resolve()
    # Installed package: vectors are bundled into the wheel at build time.
    bundled = here.parent / "_contract_vectors" / "play"
    if bundled.is_dir():
        return bundled
    # Monorepo checkout: walk up to sdk/contract/vectors/play.
    for parent in here.parents:
        candidate = parent / "contract" / "vectors" / "play"
        if candidate.is_dir():
            return candidate
    return None


def run_selftest(agent, game: str, fixtures_path: Optional[str] = None) -> list[dict]:
    if game not in agent.supported_games:
        raise SelftestFailure(f"agent has no handler for {game!r} (supported: {agent.supported_games})")

    if fixtures_path:
        path = pathlib.Path(fixtures_path)
    else:
        base = _default_fixtures_dir()
        if base is None:
            raise SelftestFailure(
                "could not locate sdk/contract/vectors/play; pass fixtures_path explicitly"
            )
        path = base / f"{game}.json"

    doc = json.loads(path.read_text())
    results = []
    for i, fx in enumerate(doc.get("fixtures", [])):
        body = {
            "type": "play_request",
            "game": fx["game"],
            "match_id": f"selftest_{game}_{i}",
            "seat": fx["seat"],
            "engine_version": fx["engine_version"],
            "state": fx["state"],
            "legal_actions": fx["legal_actions"],
            "deadline_ms": fx["deadline_ms"],
        }
        resp = agent.decide(body)
        action = resp.get("action")
        if not isinstance(action, dict) or "kind" not in action:
            raise SelftestFailure(f"fixture {i}: response is not an action dict: {resp!r}")
        _check_plausibly_legal(game, action, fx["legal_actions"], i)
        results.append({"fixture": i, "action": action})
    return results


def _check_plausibly_legal(game: str, action: dict, legal: list[str], i: int) -> None:
    """Best-effort legality check against the legal_actions encoding."""
    if not legal:
        return
    encoded = action_hint(game, action)
    if encoded is not None:
        if encoded in legal:
            return
    else:
        # Unencodable actions (bid amounts, trades): accept when the verb is
        # legal for the phase — the engine validates the parameters.
        kind = action["kind"]
        if any(h == kind or h.split(":", 1)[0] == kind for h in legal):
            return
    raise SelftestFailure(
        f"fixture {i}: action {action!r} (encoded {encoded!r}) is not in legal_actions; "
        f"a safe choice would have been {safe_action(game, legal)!r}"
    )
