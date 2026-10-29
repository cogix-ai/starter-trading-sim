"""The Agent: game handler registry + the wire-contract HTTP server.

Implements sdk/contract/ exactly:

- ``GET /health``      -> 200 {"status":"healthy","agent":name,"version":version}
- ``POST /handshake``  -> 200 {"accepted":true,"sdkVersion":...,"supportedGames":[...]}
- ``POST /play``       -> 200 {"action": <game action>}

Reliability guarantee: /play always answers something legal. If the developer's
handler raises, returns garbage, or overruns the deadline, the SDK falls back to
a safe action parsed from the platform's legal_actions hints (see fallback.py) —
because the certification sandbox scores illegal moves and transport errors
against the agent.
"""

from __future__ import annotations

import concurrent.futures
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .fallback import action_hint, safe_action

log = logging.getLogger("cogix_sdk")

KNOWN_GAMES = ("chess", "go9", "corridors", "tycoon")

# Reserve part of the budget for network + serialization overhead.
_DEADLINE_MARGIN = 0.25  # fraction of deadline_ms kept in reserve
_MIN_BUDGET_S = 0.05

# Largest request body we accept. Real play_requests are a few KB; anything
# bigger is a mistake or an attack on an internet-exposed agent.
MAX_BODY_BYTES = 1 << 20  # 1 MiB

# Per-connection socket timeout: a client that stops sending mid-request
# (slowloris) is dropped instead of pinning a server thread forever.
SOCKET_TIMEOUT_S = 15

# Upper bound on requests handled at once; excess requests get an immediate
# 503 instead of exhausting memory/threads under a connection flood.
MAX_CONCURRENT_REQUESTS = 64

# Environment variable read when Agent(token=...) is not passed, so tokens
# never need to live in agent source code.
TOKEN_ENV_VAR = "COGIX_AGENT_TOKEN"


def validate_play_request(body: dict) -> list[str]:
    """Lightweight structural check of a play_request against sdk/contract/.

    Returns a list of problems (empty = well-formed). The SDK stays permissive —
    problems are logged, not rejected — but this catches wiring mistakes early.
    """
    problems = []
    if body.get("game") not in KNOWN_GAMES:
        problems.append(f"game: {body.get('game')!r} is not one of {KNOWN_GAMES}")
    if not isinstance(body.get("match_id", ""), str):
        problems.append("match_id: expected a string")
    if not isinstance(body.get("seat", 0), int):
        problems.append("seat: expected an integer")
    if body.get("state") is not None and not isinstance(body.get("state"), dict):
        problems.append("state: expected an object")
    la = body.get("legal_actions")
    if la is not None and (not isinstance(la, list) or not all(isinstance(a, str) for a in la)):
        problems.append("legal_actions: expected a list of strings")
    dl = body.get("deadline_ms", 2000)
    if not isinstance(dl, int) or dl <= 0:
        problems.append("deadline_ms: expected a positive integer")
    return problems


@dataclass
class View:
    """One turn as the platform presents it (a parsed play_request)."""

    game: str
    match_id: str
    seat: int
    engine_version: str
    state: dict
    legal_actions: list[str] = field(default_factory=list)
    deadline_ms: int = 2000

    # -- convenience -------------------------------------------------------
    def legal_moves(self) -> list[str]:
        """legal_actions minus resign/pass — the actions that actually play."""
        return [a for a in self.legal_actions if a not in ("resign", "pass")]

    @classmethod
    def from_request(cls, body: dict) -> "View":
        def _int(v, default: int) -> int:
            # Tolerant coercion: a malformed field must degrade to its default,
            # never crash the handler (validate_play_request logs the problem).
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        state = body.get("state")
        legal = body.get("legal_actions")
        return cls(
            game=str(body.get("game", "") or ""),
            match_id=str(body.get("match_id", "") or ""),
            seat=_int(body.get("seat", 0), 0),
            engine_version=str(body.get("engine_version", "") or ""),
            state=state if isinstance(state, dict) else {},
            legal_actions=[a for a in legal if isinstance(a, str)] if isinstance(legal, list) else [],
            deadline_ms=_int(body.get("deadline_ms", 2000), 2000),
        )


Handler = Callable[[View], dict]


class Agent:
    """Register one ``decide`` function per game, then ``serve()``."""

    def __init__(self, name: str, version: str = "0.0.1", token: Optional[str] = None):
        self.name = name
        self.version = version
        # If set, /handshake and /play require this bearer token. Falls back to
        # the COGIX_AGENT_TOKEN environment variable so tokens don't need to
        # be hardcoded in agent source.
        self.token = token if token is not None else (os.environ.get(TOKEN_ENV_VAR) or None)
        self._handlers: dict[str, Handler] = {}
        # Batch handlers, keyed by challenge id. Separate from _handlers because
        # they have a different signature and lifecycle — one call per match, not
        # one per turn — but they live on the SAME Agent, because this is one SDK
        # and a developer adding a batch challenge should not start over.
        self._solvers: dict[str, Callable] = {}
        # Which of the batch handlers were registered as SESSIONS. Dispatch
        # does not need it — the first stdin frame decides the mode — but a
        # developer listing what their agent supports does (see
        # supported_sessions), and the TypeScript SDK exposes the same split.
        self._sessions: set[str] = set()
        # Adversarial handlers, keyed by challenge id: ``(obs, ctx) -> action``,
        # called once per TURN over stdio. Third entrypoint, same Agent.
        self._duelists: dict[str, Callable] = {}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    # -- registration ------------------------------------------------------
    def game(self, game_id: str) -> Callable[[Handler], Handler]:
        """Decorator: ``@agent.game("chess")`` registers the chess handler."""
        if game_id not in KNOWN_GAMES:
            raise ValueError(f"unknown game {game_id!r}; expected one of {KNOWN_GAMES}")

        def register(fn: Handler) -> Handler:
            self._handlers[game_id] = fn
            return fn

        return register

    def challenge(self, challenge_id: str) -> Callable:
        """Decorator: ``@agent.challenge("coding_duel")`` registers a batch handler.

        Batch challenges are the second of the platform's two entrypoints. The
        handler is called ONCE with ``(task, tools)`` and runs its own loop under
        a budget, rather than being called per turn like a game handler.

        The challenge id is deliberately not validated against a fixed list: new
        batch challenges ship without an SDK release, and refusing an unknown id
        here would mean every new challenge needs a new SDK version before
        anyone can enter it.
        """
        def register(fn):
            self._solvers[challenge_id] = fn
            return fn

        return register

    def session(self, challenge_id: str) -> Callable:
        """Decorator: ``@agent.session("trading_sim")`` registers an INTERACTIVE
        batch handler ``(task, tools)`` whose ``tools.market`` is iterated.

        Same wire and the same ``run_batch()`` entrypoint as ``challenge`` —
        the engine's first frame says whether a match is one-shot or a session,
        and the runtime dispatches accordingly — so registering a session with
        ``challenge`` also works. This decorator exists for parity with the
        TypeScript SDK's ``agent.session()`` and so the code says what kind of
        handler it is: one that loops over a live market rather than building
        a submission. A developer porting between the runtimes hits one API.
        """
        def register(fn):
            self._solvers[challenge_id] = fn
            self._sessions.add(challenge_id)
            return fn

        return register

    def run_batch(self, stdin=None, stdout=None) -> int:
        """Run one batch match: read the task from stdin, write the submission
        to stdout. This is the entrypoint a batch agent's build invokes.
        """
        from .batch import run_batch as _run

        return _run(self._solvers, stdin=stdin, stdout=stdout)

    def duel(self, challenge_id: str) -> Callable:
        """Decorator: ``@agent.duel("ctf_attack_defend")`` registers an adversarial
        handler ``(obs, ctx) -> action``.

        Adversarial challenges are the third entrypoint: two live agents act
        through an engine-mediated environment, roles swapping between rounds.
        The handler runs once per turn; ``obs.role`` says which job it has THIS
        turn (never cache it). See ``cogix_sdk.duel`` for the rules the runtime
        enforces on its behalf.
        """
        def register(fn):
            self._duelists[challenge_id] = fn
            return fn

        return register

    def run_duel(self, stdin=None, stdout=None, default: Optional[Callable] = None) -> None:
        """Run one adversarial match over stdin/stdout. This is the entrypoint
        an adversarial agent's build invokes.
        """
        from .duel import run_duel as _run

        _run(self._duelists, stdin=stdin, stdout=stdout, default=default)

    @property
    def supported_duels(self) -> list[str]:
        return sorted(self._duelists)

    @property
    def supported_challenges(self) -> list[str]:
        """Every batch challenge id, sessions included (one entrypoint)."""
        return sorted(self._solvers)

    @property
    def supported_sessions(self) -> list[str]:
        """The batch challenges registered with ``session``."""
        return sorted(self._sessions)

    @property
    def supported_games(self) -> list[str]:
        return sorted(self._handlers)

    # -- wire behaviour (used by the server and by tests) ------------------
    def health_body(self) -> dict:
        return {"status": "healthy", "agent": self.name, "version": self.version}

    def handshake_body(self) -> dict:
        from . import __version__

        return {
            "accepted": True,
            "sdkVersion": f"cogix-sdk-py/{__version__}",
            "supportedGames": self.supported_games,
        }

    def decide(self, body: dict) -> dict:
        """Answer one play_request body with a play_response body — never raises."""
        for problem in validate_play_request(body):
            log.warning("malformed play_request: %s", problem)
        view = View.from_request(body)
        fallback = safe_action(view.game, view.legal_actions) or {"kind": "resign"}

        handler = self._handlers.get(view.game)
        if handler is None:
            log.warning("no handler for game %s; using fallback", view.game)
            return {"action": fallback}

        budget = max(view.deadline_ms / 1000.0 * (1 - _DEADLINE_MARGIN), _MIN_BUDGET_S)
        try:
            action = self._pool.submit(handler, view).result(timeout=budget)
        except concurrent.futures.TimeoutError:
            log.warning("%s handler overran %.0fms budget; using fallback", view.game, budget * 1000)
            return {"action": fallback}
        except Exception:
            log.exception("%s handler raised; using fallback", view.game)
            return {"action": fallback}

        if not isinstance(action, dict) or "kind" not in action:
            log.warning("%s handler returned %r (not an action dict); using fallback", view.game, action)
            return {"action": fallback}

        # Local legality check: warn (but still send) when the handler's action
        # is verifiably absent from legal_actions — the platform will score it.
        hint = action_hint(view.game, action)
        if view.legal_actions and hint is not None and hint not in view.legal_actions:
            log.warning(
                "%s handler returned %r which is not in legal_actions %r; "
                "sending anyway — the certification sandbox will score this against you",
                view.game, action, view.legal_actions,
            )
        return {"action": action}

    # -- serving -----------------------------------------------------------
    def serve(self, host: str = "0.0.0.0", port: int = 8700) -> None:
        """Serve the wire contract (blocking). Ctrl-C to stop."""
        if not self.token and host in ("0.0.0.0", "::"):
            log.warning(
                "serving on %s WITHOUT a bearer token — anyone who can reach this "
                "port can drive your agent. Pass Agent(token=...) or bind to "
                "127.0.0.1 behind a TLS tunnel (see README: 'Serving securely').",
                host,
            )
        server = self.build_server(host, port)
        log.info("%s serving %s on %s:%d", self.name, self.supported_games, host, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()

    def build_server(self, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
        """Build (but don't start) the HTTP server — tests use this."""
        agent = self
        # Bounds in-flight requests: a connection flood gets fast 503s instead
        # of exhausting threads/memory. Exposed on the server for tests.
        sem = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

        class _RequestHandler(BaseHTTPRequestHandler):
            timeout = SOCKET_TIMEOUT_S  # drop slowloris-style stalled connections

            def log_message(self, fmt, *args):  # route through logging, not stderr
                log.debug("%s " + fmt, self.address_string(), *args)

            def handle_one_request(self):  # guard every request with the cap
                if not sem.acquire(blocking=False):
                    try:
                        # send_response before parse_request: give it the
                        # attributes its logging path expects.
                        self.requestline, self.request_version, self.command = "", "HTTP/1.1", ""
                        self.close_connection = True
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        body = b'{"error": "server busy"}'
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
                try:
                    super().handle_one_request()
                finally:
                    sem.release()

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if not agent.token:
                    return True
                got = self.headers.get("Authorization", "").encode()
                want = f"Bearer {agent.token}".encode()
                return hmac.compare_digest(got, want)  # constant-time

            def _content_length(self) -> Optional[int]:
                # A NEGATIVE length is not a length. int("-1") parses happily,
                # -1 is not greater than the cap below, and rfile.read(-1)
                # reads to EOF — so `Content-Length: -1` walked straight past
                # the 1 MiB limit and let a caller send as much as it liked
                # into a developer's agent.
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    return None
                return n if n >= 0 else None

            def _read_json(self, n: int) -> Optional[dict]:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    return body if isinstance(body, dict) else None
                except (ValueError, json.JSONDecodeError, RecursionError):
                    # RecursionError: pathologically nested JSON must be a clean
                    # 400, not a crashed handler thread.
                    return None

            def do_GET(self):
                if self.path.rstrip("/").endswith("health") or self.path == "/":
                    self._send(200, agent.health_body())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    self._send(401, {"error": "missing or bad bearer token"})
                    return
                n = self._content_length()
                if n is None:
                    self._send(400, {"error": "invalid Content-Length"})
                    return
                if n > MAX_BODY_BYTES:
                    self._send(413, {"error": f"request body over {MAX_BODY_BYTES} bytes"})
                    return
                body = self._read_json(n)
                if body is None:
                    self._send(400, {"error": "invalid JSON"})
                    return
                path = self.path.rstrip("/")
                if path.endswith("handshake"):
                    self._send(200, agent.handshake_body())
                elif path.endswith("play") or body.get("type") == "play_request":
                    self._send(200, agent.decide(body))
                else:
                    self._send(404, {"error": "not found"})

        server = ThreadingHTTPServer((host, port), _RequestHandler)
        server.cogix_semaphore = sem  # type: ignore[attr-defined] — for tests/introspection
        return server

    # -- local selftest ----------------------------------------------------
    def selftest(self, game: str, fixtures_path: Optional[str] = None) -> list[dict]:
        """Replay the contract's golden play_requests for ``game`` through this
        agent's handler and report each answer. Raises if any answer is not a
        well-formed action drawn from the fixture's legal_actions."""
        from .selftest import run_selftest

        return run_selftest(self, game, fixtures_path)
