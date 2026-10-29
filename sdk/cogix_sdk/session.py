"""The interactive session: challenges the agent has a CONVERSATION with.

Most batch challenges are one-shot — the engine writes a task, the agent writes
an artifact, done. Some are not. A trading agent has to loop: observe the market,
place orders, advance the clock, observe again. So does any interactive challenge.

The wire is the same JSON lines over the same stdio, so nothing new is exposed
and the sandbox keeps deny-by-default egress::

    ENGINE -> AGENT  {"type":"session_start","task":{...}}
    ENGINE -> AGENT  {"type":"observation","obs":{...}}
    AGENT  -> ENGINE {"type":"actions","actions":[...]}
    ENGINE -> AGENT  {"type":"results","results":[...],"obs":{...}}   ...repeat
    ENGINE -> AGENT  {"type":"session_end","final_equity":...}

The developer never writes that. They write a loop::

    @agent.challenge("trading_sim")
    def trade(task, tools):
        for obs in tools.market:
            if obs["bar_index"] == 0:
                tools.market.place(instrument="SYN1", side="buy", type="market", qty=10)

Iterating sends whatever was queued and reads the next observation. One round
trip per bar, whether the agent placed six orders or none — which is deliberate:
if the round-trip count depended on how busy an agent was, the tool-call budget
would cap strategy complexity rather than effort, and the busier agent would
advance the clock later on a metric that breaks ties.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional

log = logging.getLogger("cogix_sdk")

# Frame types, mirroring the engine (backend/internal/challenge/tradingsim/wire.go).
FRAME_SESSION_START = "session_start"
FRAME_OBSERVATION = "observation"
FRAME_ACTIONS = "actions"
FRAME_RESULTS = "results"
FRAME_SESSION_END = "session_end"
FRAME_ERROR = "error"

# Action kinds.
ACTION_PLACE = "place"
ACTION_CANCEL = "cancel"
ACTION_HOLD = "hold"
ACTION_DONE = "done"

# Retrieval session frame types (research_race), mirroring the engine
# (backend/internal/challenge/researchrace/retrieval_wire.go).
FRAME_SEARCH_RESULT = "search_result"
FRAME_FETCH_RESULT = "fetch_result"
AGENT_SEARCH = "search"
AGENT_FETCH = "fetch"
AGENT_SUBMIT = "submit"
AGENT_DONE = "done"


class SessionClosed(RuntimeError):
    """The engine ended the session. Whatever was already done still counts."""


class BudgetExhausted(RuntimeError):
    """The retrieval-call budget is spent. The engine will serve no more search
    or fetch calls; whatever the agent found already is all it gets to answer
    from."""


class Market:
    """The interactive surface for a session challenge.

    Iterate it to walk the match forward. Queue actions between iterations; they
    are sent when the loop advances, which is also when the clock advances.
    """

    def __init__(self, transport: "LineTransport"):
        self._t = transport
        self._pending: list[dict] = []
        self._results: list[dict] = []
        self._last_obs: Optional[dict] = None
        self._ended = False
        self._final: Optional[dict] = None

    # -- iteration ---------------------------------------------------------
    def __iter__(self) -> Iterator[dict]:
        """Yield each observation in turn, flushing queued actions between them."""
        while True:
            obs = self._advance()
            if obs is None:
                return
            yield obs

    def _advance(self) -> Optional[dict]:
        # The first call consumes the observation the engine already sent; every
        # later call answers it first.
        if self._last_obs is not None or self._pending or self._results:
            if not self._send_actions():
                return None
        frame = self._t.recv()
        return self._absorb(frame)

    def _send_actions(self) -> bool:
        actions = self._pending or [{"kind": ACTION_HOLD}]
        self._pending = []
        try:
            self._t.send({"type": FRAME_ACTIONS, "actions": actions})
        except SessionClosed:
            self._ended = True
            return False
        return True

    def _absorb(self, frame: Optional[dict]) -> Optional[dict]:
        if frame is None:
            self._ended = True
            return None
        kind = frame.get("type")
        if kind == FRAME_SESSION_END:
            self._ended = True
            self._final = frame
            return None
        if kind == FRAME_ERROR:
            # The engine rejected a frame. It is telling us why so the developer
            # can fix it; the session is over either way.
            log.error("session error from the engine: %s", frame.get("message"))
            self._ended = True
            return None
        self._results = frame.get("results") or []
        self._last_obs = frame.get("obs")
        return self._last_obs

    # -- actions -----------------------------------------------------------
    def place(self, **order) -> None:
        """Queue an order for this bar. Sent when the loop advances."""
        self._pending.append({"kind": ACTION_PLACE, "order": dict(order)})

    def cancel(self, order_id: int) -> None:
        """Queue a cancellation of a resting order."""
        self._pending.append({"kind": ACTION_CANCEL, "order_id": int(order_id)})

    def done(self) -> None:
        """End the session early.

        Whatever was built is KEPT: the engine plays out the remaining bars and
        marks the book at the final close. Stopping early is a strategy, not a
        way to freeze the mark at a favourable bar.
        """
        self._pending.append({"kind": ACTION_DONE})

    # -- state -------------------------------------------------------------
    @property
    def results(self) -> list[dict]:
        """Results of the actions sent on the PREVIOUS iteration, in order.

        Correlation is positional: result i answers action i. There are no ids
        because the engine only ever speaks in reply, so nothing can arrive out
        of order.
        """
        return list(self._results)

    @property
    def observation(self) -> Optional[dict]:
        """The most recent observation."""
        return self._last_obs

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def final(self) -> Optional[dict]:
        """The session_end frame, once the match is over."""
        return self._final


class Library:
    """The interactive surface for Research Race: the frozen corpus, reached by
    SEARCHING it rather than reading files.

    Each call is one retrieval call against the task's tool-call budget, so query
    planning is the skill — ``search`` returns the ranked hits, ``fetch`` reads a
    document in full, and ``submit`` (or simply returning a dict from the handler)
    ends the match with an answer::

        @agent.challenge("research_race")
        def solve(task, tools):
            hits = tools.library.search("qubits decoherence", k=5)
            doc = tools.library.fetch(hits[0]["doc_id"])
            return {"answer": "...", "citations": [{"doc_id": hits[0]["doc_id"]}]}

    When the budget runs out, ``search``/``fetch`` raise ``BudgetExhausted`` so a
    loop stops cleanly instead of reading an empty result as "nothing matched".
    """

    def __init__(self, transport: "LineTransport"):
        self._t = transport
        self._submitted = False
        self._ended = False
        self._remaining: Optional[int] = None

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Rank the corpus for a query; returns a list of {doc_id, score, snippet}."""
        self._t.send({"type": AGENT_SEARCH, "query": str(query), "k": int(k)})
        frame = self._reply(self._t.recv())
        return list(frame.get("hits") or [])

    def fetch(self, doc_id: str) -> dict:
        """Read one document in full; returns {doc_id, text, found}."""
        self._t.send({"type": AGENT_FETCH, "doc_id": str(doc_id)})
        frame = self._reply(self._t.recv())
        return {"doc_id": frame.get("doc_id", doc_id), "text": frame.get("text", ""), "found": bool(frame.get("found"))}

    def submit(self, answer: Any, citations: Optional[list] = None) -> None:
        """End the match with an answer. Calling it twice, or after the handler
        returned a dict, is harmless — the first submission that reaches the
        engine is the one it grades."""
        if self._submitted or self._ended:
            return
        self._t.send({"type": AGENT_SUBMIT, "answer": answer, "citations": list(citations or [])})
        self._submitted = True
        # Consume the engine's session_end so it is not left as a broken pipe.
        try:
            self._absorb_end(self._t.recv())
        except SessionClosed:
            pass

    def done(self) -> None:
        """Give up without an answer — distinct from a crash in the record."""
        if self._submitted or self._ended:
            return
        self._t.send({"type": AGENT_DONE})
        self._ended = True
        try:
            self._absorb_end(self._t.recv())
        except SessionClosed:
            pass

    @property
    def calls_remaining(self) -> Optional[int]:
        """Retrieval calls left after the last call; -1 means unlimited, None
        means no call has been made yet."""
        return self._remaining

    @property
    def submitted(self) -> bool:
        return self._submitted

    @property
    def ended(self) -> bool:
        return self._ended

    # -- internals ---------------------------------------------------------
    def _reply(self, frame: Optional[dict]) -> dict:
        if frame is None:
            self._ended = True
            raise SessionClosed("engine closed the session")
        kind = frame.get("type")
        if kind == FRAME_ERROR:
            self._remaining = frame.get("calls_remaining")
            msg = str(frame.get("message") or "")
            if self._remaining == 0 or "budget" in msg.lower():
                raise BudgetExhausted(msg or "retrieval budget exhausted")
            # Any other protocol complaint ends the session rather than looping.
            self._ended = True
            raise SessionClosed(msg or "engine rejected the frame")
        if kind == FRAME_SESSION_END:
            self._ended = True
            raise SessionClosed("session ended")
        if "calls_remaining" in frame:
            self._remaining = frame.get("calls_remaining")
        return frame

    def _absorb_end(self, frame: Optional[dict]) -> None:
        self._ended = True
        # session_end (or a close) is expected here; nothing to read out of it.


class LineTransport:
    """JSON lines over a pair of streams."""

    def __init__(self, stdin, stdout):
        self._in = stdin
        self._out = stdout

    def send(self, frame: dict) -> None:
        # Canonical encoding, same as the one-shot path: sorted keys and no
        # whitespace, so the Python and TypeScript runtimes emit byte-identical
        # frames for the same logical action.
        body = json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        try:
            self._out.write(body + "\n")
            self._out.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise SessionClosed("engine closed the session") from exc

    def recv(self) -> Optional[dict]:
        line = self._in.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            log.error("engine sent a frame that is not valid JSON")
            return None


def run_session(handlers: dict, first: dict, stdin, stdout) -> int:
    """Run one interactive match. Never raises.

    ``first`` is the already-parsed session_start frame; the caller read it to
    decide the mode.

    A handler that raises or a challenge with no handler both end the session
    quietly rather than exiting non-zero: the engine scores whatever the agent
    already did, and a non-zero exit would be indistinguishable from a platform
    fault — which scores nobody and would void the opponent's legitimate work.
    """
    transport = LineTransport(stdin, stdout)
    task = first.get("task") or {}
    challenge_id = str(task.get("challenge_id") or "")
    handler = handlers.get(challenge_id)

    market = Market(transport)
    if handler is None:
        log.error("no solve handler registered for challenge %r", challenge_id)
        return 0

    tools = SessionTools(task, market, transport)
    ret = None
    try:
        ret = handler(task, tools)
    except SessionClosed:
        pass
    except Exception:  # noqa: BLE001 - a handler bug must not discard the work it already did
        log.exception("solve handler raised; the engine keeps what was already done")

    # A retrieval match (the handler touched tools.library) ends with a submit,
    # not a market drain. If the handler returned an answer dict and did not
    # submit it itself, submit it for them; otherwise close with whatever it had.
    if tools.used_library:
        lib = tools.library
        if not lib.submitted and not lib.ended:
            sub = ret if isinstance(ret, dict) else {}
            try:
                lib.submit(sub.get("answer"), sub.get("citations"))
            except SessionClosed:
                pass
        return 0

    # Drain politely so the engine's final frame is consumed rather than
    # arriving as a broken pipe on its side.
    if not market.ended:
        try:
            market._send_actions()  # noqa: SLF001 - internal drain, deliberate
            market._absorb(transport.recv())  # noqa: SLF001
        except SessionClosed:
            pass
    return 0


class SessionTools:
    """What ``solve`` is given for a session challenge.

    Deliberately the same shape as the one-shot ``Tools``: ``task`` plus the
    surface the challenge needs. A developer moving between challenges learns
    one object, not two.
    """

    def __init__(self, task: dict, market: Market, transport: "Optional[LineTransport]" = None):
        self.task = task
        self.market = market
        self._transport = transport
        self._library: Optional[Library] = None

    @property
    def library(self) -> Library:
        """The retrieval surface for Research Race. Built on first access, which
        is also how the runtime learns this is a retrieval match rather than a
        market one, so it submits on the handler's return instead of draining the
        market."""
        if self._library is None:
            self._library = Library(self._transport)
        return self._library

    @property
    def used_library(self) -> bool:
        return self._library is not None

    @property
    def budget(self) -> dict:
        """The symmetric limits, straight from the task."""
        return self.task.get("budgets") or {}


def looks_like_session(frame: Any) -> bool:
    """True when the first stdin frame opens an interactive session."""
    return isinstance(frame, dict) and frame.get("type") == FRAME_SESSION_START
