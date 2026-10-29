"""Typed, read-only views over each game's state — autocomplete-friendly
wrappers around the raw ``view.state`` dict the platform sends.

Usage::

    from cogix_sdk import games

    @agent.game("chess")
    def play(view):
        chess = games.Chess(view)
        if chess.in_check:
            ...
        return {"kind": "move", "move": view.legal_moves()[0]}

The state shapes come from the Go engines (the source of truth); real captures
live in sdk/contract/vectors/play/. Fields default sensibly if absent, so these
wrappers never raise on a well-formed platform request.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent import View


class _GameView:
    """Shared plumbing: wraps a View, exposes raw state access."""

    def __init__(self, view: View):
        self.view = view
        self.state: dict = view.state

    def raw(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    @property
    def seat(self) -> int:
        return self.view.seat

    @property
    def finished(self) -> bool:
        return bool(self.state.get("finished", False))

    @property
    def winner(self) -> int:
        return int(self.state.get("winner", -1))


class Chess(_GameView):
    """Chess state. Board: 64 ints, a1=0..h8=63; positive = white pieces
    (1 pawn, 2 knight, 3 bishop, 4 rook, 5 queen, 6 king), negative = black."""

    @property
    def board(self) -> list[int]:
        return list(self.state.get("board") or [])

    @property
    def turn(self) -> int:
        """0 = white to move, 1 = black."""
        return int(self.state.get("turn", 0))

    @property
    def in_check(self) -> bool:
        return bool(self.state.get("in_check", False))

    @property
    def halfmove_clock(self) -> int:
        return int(self.state.get("halfmove_clock", 0))

    @property
    def full_move(self) -> int:
        return int(self.state.get("full_move", 1))

    def piece_at(self, square: str) -> int:
        """Piece code at an algebraic square, e.g. ``piece_at("e4")``."""
        col = ord(square[0]) - ord("a")
        row = int(square[1]) - 1
        board = self.state.get("board") or []
        idx = row * 8 + col
        return int(board[idx]) if 0 <= idx < len(board) else 0


class Go9(_GameView):
    """9x9 Go state. Board: 81 ints row-major, 0 empty, 1 black, 2 white."""

    SIZE = 9

    @property
    def board(self) -> list[int]:
        return list(self.state.get("board") or [])

    @property
    def turn(self) -> int:
        return int(self.state.get("turn", 0))

    @property
    def passes(self) -> int:
        return int(self.state.get("passes", 0))

    @property
    def prisoners(self) -> list[int]:
        return list(self.state.get("prisoners") or [0, 0])

    def stone_at(self, x: int, y: int) -> int:
        board = self.state.get("board") or []
        idx = y * self.SIZE + x
        return int(board[idx]) if 0 <= idx < len(board) else 0


class Corridors(_GameView):
    """Corridors state: pawn positions, walls remaining, placed walls."""

    @property
    def pawns(self) -> list[dict]:
        """One {"x","y"} per seat."""
        return list(self.state.get("pawns") or [])

    @property
    def my_pawn(self) -> Optional[dict]:
        pawns = self.pawns
        return pawns[self.seat] if self.seat < len(pawns) else None

    @property
    def walls_left(self) -> list[int]:
        return list(self.state.get("walls_left") or [])

    @property
    def walls(self) -> list[dict]:
        """Placed walls: {"x","y","dir"} each."""
        return list(self.state.get("walls") or [])

    @property
    def turn(self) -> int:
        return int(self.state.get("turn", 0))


class Tycoon(_GameView):
    """Tycoon state: players, holdings, phase, and helpers for my seat."""

    @property
    def players(self) -> list[dict]:
        return list(self.state.get("players") or [])

    @property
    def holdings(self) -> list[dict]:
        """Per board index: owner / houses / mortgaged."""
        return list(self.state.get("holdings") or [])

    @property
    def phase(self) -> str:
        return str(self.state.get("phase", ""))

    @property
    def current(self) -> int:
        return int(self.state.get("current", 0))

    @property
    def last_roll(self) -> list[int]:
        return list(self.state.get("last_roll") or [])

    @property
    def me(self) -> Optional[dict]:
        players = self.players
        return players[self.seat] if self.seat < len(players) else None

    @property
    def cash(self) -> int:
        me = self.me
        return int(me.get("cash", 0)) if isinstance(me, dict) else 0

    @property
    def position(self) -> int:
        me = self.me
        return int(me.get("position", 0)) if isinstance(me, dict) else 0

    @property
    def in_jail(self) -> bool:
        me = self.me
        return bool(me.get("in_jail", False)) if isinstance(me, dict) else False

    def my_properties(self) -> list[int]:
        """Board indexes I own."""
        return [i for i, h in enumerate(self.holdings)
                if isinstance(h, dict) and h.get("owner") == self.seat]
