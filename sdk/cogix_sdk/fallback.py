"""Deadline-safe fallback: derive a legal action from ``legal_actions``.

The certification sandbox rejects agents that answer illegally, time out, or
resign/pass-spam. If the developer's handler fails, the SDK answers with the
first *playing* action (not resign/pass) parsed from the platform's own
``legal_actions`` hints — the same encodings the Go engines emit
(see sdk/contract/play_request.schema.json).
"""

from __future__ import annotations

from typing import Optional

# Actions that end the agent's game rather than advance it. Legal, but only ever
# the right answer when nothing else is.
_TYCOON_LAST_RESORT = {"bankrupt"}

_TYCOON_SAFE = {
    "roll", "buy", "decline", "pass", "pay_jail", "use_jail_card",
    "roll_jail", "end_turn", "bankrupt", "accept_trade", "reject_trade",
}


def safe_action(game: str, legal_actions: list[str]) -> Optional[dict]:
    # NOTE: tycoon delegates to parse_hint, defined further down. Resolution
    # happens at call time, so the order in this file does not matter.
    """Return a legal, engine-accepted action dict, or None if none can be derived."""
    if game == "chess":
        for s in legal_actions:
            if s != "resign":
                return {"kind": "move", "move": s}
        return {"kind": "resign"} if "resign" in legal_actions else None

    if game == "go9":
        for s in legal_actions:
            if s not in ("pass", "resign"):
                xy = _parse_xy(s)
                if xy:
                    return {"kind": "move", "x": xy[0], "y": xy[1]}
        return {"kind": "pass"} if "pass" in legal_actions else None

    if game == "corridors":
        for s in legal_actions:
            if s.startswith("move:"):
                xy = _parse_xy(s[5:])
                if xy:
                    return {"kind": "move", "to": {"x": xy[0], "y": xy[1]}}
        for s in legal_actions:
            if s.startswith("wall:"):
                parts = s[5:].split(",")
                if len(parts) == 3:
                    xy = _parse_xy(",".join(parts[:2]))
                    if xy and parts[2] in ("h", "v"):
                        return {"kind": "wall", "wall": {"x": xy[0], "y": xy[1], "dir": parts[2]}}
        return {"kind": "resign"} if "resign" in legal_actions else None

    if game == "tycoon":
        # Three passes, in order of preference.
        #
        # 1. A plain playing action. 2. A PARAMETERISED one (mortgage:12,
        #    bid:51, …) — these used to be skipped entirely, so whole phases had
        #    no fallback at all: certification against this helper failed 979 of
        #    2103 decisions. 3. Only then the give-up actions.
        #
        # The ordering is the point as much as the parsing. In the debt phase the
        # legal set is {mortgage:…, sell_house:…, bankrupt}; taking the first
        # match from a flat set meant answering "bankrupt" while a mortgage was
        # available — a legal move that loses the game on purpose.
        for s in legal_actions:
            if s in _TYCOON_SAFE and s not in _TYCOON_LAST_RESORT:
                return {"kind": s}
        for s in legal_actions:
            if ":" not in s:
                continue
            act = parse_hint("tycoon", s)
            if act is not None:
                return act
        for s in legal_actions:
            if s in _TYCOON_LAST_RESORT:
                return {"kind": s}
        return None

    return None


def action_hint(game: str, action: dict) -> Optional[str]:
    """Encode an action dict back to its ``legal_actions`` hint string.

    The inverse of :func:`safe_action`'s parsing. Returns None when the action
    can't be encoded (unknown kind, parameterised tycoon actions) — in that
    case local legality can't be checked and the platform is the judge.
    """
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if kind == "resign":
        return "resign"

    if game == "chess":
        return action.get("move") if kind == "move" and isinstance(action.get("move"), str) else None

    if game == "go9":
        if kind == "pass":
            return "pass"
        if kind == "move":
            # A missing coordinate means zero, not "absent". The engine used to
            # encode this action with omitempty, so every move on row 0 or
            # column 0 — 17 of the 81 points — reached us without one, and
            # requiring both made a legal corner play unencodable and therefore
            # "not in legal_actions". The engine writes both now; this still
            # reads the logs recorded before it did.
            x, y = action.get("x", 0), action.get("y", 0)
            if isinstance(x, int) and isinstance(y, int) and not isinstance(x, bool) and not isinstance(y, bool):
                return f"{x},{y}"
        return None

    if game == "corridors":
        if kind == "move":
            to = action.get("to") or {}
            if isinstance(to.get("x"), int) and isinstance(to.get("y"), int):
                return f"move:{to['x']},{to['y']}"
            return None
        if kind == "wall":
            w = action.get("wall") or {}
            if isinstance(w.get("x"), int) and isinstance(w.get("y"), int) and w.get("dir") in ("h", "v"):
                return f"wall:{w['x']},{w['y']},{w['dir']}"
            return None
        return None

    if game == "tycoon":
        if not isinstance(kind, str):
            return None
        # Parameter-less kinds map 1:1 to hints.
        if set(action) == {"kind"}:
            return kind
        # Property verbs are parameterized since tycoon-1.2.0: "build:19".
        if kind in ("build", "sell_house", "mortgage", "unmortgage") \
                and isinstance(action.get("property"), int) and set(action) <= {"kind", "property"}:
            return f"{kind}:{action['property']}"
        return None  # bid amounts and trades can't be hint-checked exactly

    return None


_TYCOON_PROPERTY_VERBS = ("build", "sell_house", "mortgage", "unmortgage")


def parse_hint(game: str, hint: str) -> Optional[dict]:
    """Parse one ``legal_actions`` hint string into a ready-to-send action dict.

    The inverse of :func:`action_hint`. Every hint the platform lists parses to
    an action the engine is guaranteed to accept. Returns None for hints that
    need agent-chosen parameters (``propose_trade``) or malformed input.
    """
    if not isinstance(hint, str) or not hint:
        return None
    if hint == "resign":
        return {"kind": "resign"}

    if game == "chess":
        return {"kind": "move", "move": hint}

    if game == "go9":
        if hint == "pass":
            return {"kind": "pass"}
        xy = _parse_xy(hint)
        return {"kind": "move", "x": xy[0], "y": xy[1]} if xy else None

    if game == "corridors":
        if hint.startswith("move:"):
            xy = _parse_xy(hint[5:])
            return {"kind": "move", "to": {"x": xy[0], "y": xy[1]}} if xy else None
        if hint.startswith("wall:"):
            parts = hint[5:].split(",")
            if len(parts) == 3 and parts[2] in ("h", "v"):
                xy = _parse_xy(",".join(parts[:2]))
                if xy:
                    return {"kind": "wall", "wall": {"x": xy[0], "y": xy[1], "dir": parts[2]}}
        return None

    if game == "tycoon":
        verb, _, arg = hint.partition(":")
        if not arg:
            return {"kind": verb} if verb != "propose_trade" else None
        try:
            n = int(arg)
        except ValueError:
            return None
        if verb in _TYCOON_PROPERTY_VERBS:
            return {"kind": verb, "property": n}
        if verb == "bid":
            return {"kind": "bid", "amount": n}
        return None

    return None


def _parse_xy(s: str) -> Optional[tuple[int, int]]:
    parts = s.split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None
