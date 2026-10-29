"""The batch entrypoint: ``solve(task, tools)``.

The platform has exactly two agent-facing entrypoints and this is the second one
(00FOUNDATION B1). Interactive challenges — the four games — drive ``act``/the
per-game handlers over HTTP and are unchanged. Batch challenges call ``solve``
ONCE and let the agent run its own loop under a budget::

    from cogix_sdk import Agent

    agent = Agent(name="my-agent", version="1.0.0")

    @agent.challenge("coding_duel")
    def solve_it(task, tools):
        tools.write_file(task["entrypoint"], my_solution(task["spec"]))

    agent.run_batch()

This is one SDK, not two. A developer who already has a game agent adds a
decorator; they do not start over.

The wire is deliberately the simplest thing that can be sandboxed: the engine
writes the task as JSON to stdin, the agent writes its submission as JSON to
stdout. No socket, no port, no ambient network — which matters because a batch
sandbox runs with deny-by-default egress and the agent is untrusted code.

Contract: ``sdk/contract/batch_task.schema.json`` and
``batch_submission.schema.json``, with conformance fixtures under
``sdk/contract/vectors/batch/`` that both the Python and TypeScript runtimes
replay in CI and must answer byte-identically.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Callable, Optional

from .session import looks_like_session, run_session

log = logging.getLogger("cogix_sdk")

# Reserve part of the wall budget so a solve() that runs right up to the line
# still has time to serialize and flush its submission. An agent killed mid-write
# submits nothing, which is the worst possible outcome for work it actually did.
_WALL_MARGIN = 0.10  # fraction of wall_seconds kept in reserve
_MIN_RESERVE_S = 1.0

# Submission caps, mirroring the engine's own (spec 01 §4.5). Enforced here too
# so an agent learns at write time rather than scoring zero on a bundle the
# grader refuses.
MAX_BUNDLE_FILES = 64
MAX_BUNDLE_PATH_LEN = 200
MAX_SUBMISSION_BYTES = 1 << 20  # 1 MiB


class BudgetExhausted(RuntimeError):
    """Raised by ``tools.checkpoint()`` when the wall budget is spent."""


class Budget:
    """The symmetric limits this match runs under, and the clock against them.

    Both seats receive identical budgets, so nothing here is secret. The point of
    exposing it is that an agent which cannot see its budget cannot spend it
    well, and a leaderboard that measures guesswork about deadlines is measuring
    the wrong thing.
    """

    def __init__(self, budgets: Optional[dict] = None, clock: Callable[[], float] = time.monotonic):
        b = budgets or {}
        self.wall_seconds: int = _int(b.get("wall_seconds"), 0)
        self.tokens: int = _int(b.get("tokens"), 0)
        self.tool_calls: int = _int(b.get("tool_calls"), 0)
        self._clock = clock
        self._start = clock()
        # Counted locally. The authoritative count is the metered proxy's; this
        # is the agent's own view so it can pace itself between calls.
        self.tokens_used = 0
        self.tool_calls_used = 0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        """Seconds left before the reserve. ``inf`` when no wall budget is set."""
        if self.wall_seconds <= 0:
            return float("inf")
        reserve = max(self.wall_seconds * _WALL_MARGIN, _MIN_RESERVE_S)
        return max(0.0, self.wall_seconds - reserve - self.elapsed)

    def exhausted(self) -> bool:
        """True when any budget is spent. The idiomatic loop guard."""
        if self.remaining() <= 0:
            return True
        if self.tokens > 0 and self.tokens_used >= self.tokens:
            return True
        if self.tool_calls > 0 and self.tool_calls_used >= self.tool_calls:
            return True
        return False

    def spend(self, tokens: int = 0, tool_calls: int = 0) -> None:
        """Record local usage so ``exhausted()`` reflects it."""
        self.tokens_used += max(0, _int(tokens, 0))
        self.tool_calls_used += max(0, _int(tool_calls, 0))

    def as_dict(self) -> dict:
        return {
            "wall_seconds": self.wall_seconds,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "elapsed": round(self.elapsed, 3),
            "tokens_used": self.tokens_used,
            "tool_calls_used": self.tool_calls_used,
        }


class SubmitResult:
    """What ``submit_report`` answers: ``{accepted, seq, reason}``."""

    __slots__ = ("accepted", "seq", "reason")

    def __init__(self, accepted: bool, seq: Optional[int], reason: str = ""):
        self.accepted = accepted
        self.seq = seq
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SubmitResult(accepted={self.accepted}, seq={self.seq}, reason={self.reason!r})"

    def __getitem__(self, key: str):
        """Dict-style access, so ``res["accepted"]`` reads like the spec."""
        return {"accepted": self.accepted, "seq": self.seq, "reason": self.reason}[key]


class Tools:
    """Everything ``solve`` is given besides the task.

    Three submission surfaces, one per challenge shape:

    - ``write_file(path, content)`` builds a FILE BUNDLE (Coding Duel).
    - ``submit_report(report)`` builds a REPORT LIST (Bug Hunt Race).
    - ``answer(value, citations=None)`` / ``code(source)`` / ``submit(obj)``
      set a single DOCUMENT (Research Race ``{"answer","citations"}``,
      Speed/Efficiency ``{"code"}`` or ``{"answer"}``). ``solve()`` may also
      simply ``return`` the document; it is used when nothing else was
      submitted.

    A challenge uses exactly one; mixing them is a programming error and
    raises, because the engine reads one artifact and would silently drop the
    other half.

    Rejections are FREE. A refused report costs the agent nothing but the call —
    no cap slot, no score. Punishing malformed submissions would make an agent
    decline to report a real finding because a typo might cost it points.
    """

    def __init__(self, task: dict, budget: Optional[Budget] = None):
        self.task = task
        self.budget = budget if budget is not None else Budget(task.get("budgets"))
        self._files: dict[str, str] = {}
        self._reports: list[dict] = []
        # The single-document surface. None until answer()/code()/submit() is
        # called; a later call REPLACES the document (an agent refining its
        # answer should not have to clear the old one first).
        self._doc: Any = None
        self._rejections: list[dict] = []
        self._seq = 0
        # Cap feedback has to be immediate: an agent that learns at scoring time
        # that its last twenty reports were discarded has already spent budget
        # producing them.
        self._max_reports = _int(task.get("max_reports"), 0)
        self._taxonomy = task.get("bug_taxonomy") if isinstance(task.get("bug_taxonomy"), list) else None

    def _guard(self, surface: str) -> None:
        """Refuse to mix surfaces: the engine reads ONE artifact shape."""
        used = []
        if self._files and surface != "files":
            used.append("files")
        if self._reports and surface != "reports":
            used.append("reports")
        if self._doc is not None and surface != "document":
            used.append("a document")
        if used:
            raise RuntimeError(
                f"this submission already contains {', '.join(used)}; use one surface, not both"
            )

    # -- single document (Research Race, Speed/Efficiency) -------------------
    def submit(self, document: Any) -> None:
        """Set the whole submission to ``document`` (a JSON-serialisable object).

        The generic form of :meth:`answer` and :meth:`code`. Replaces any
        earlier document, so refine freely; the last one wins.
        """
        self._guard("document")
        if not isinstance(document, (dict, list)):
            raise ValueError("submission document must be a JSON object or array")
        self._doc = document

    def answer(self, value: Any, citations: Optional[list] = None, **extra: Any) -> None:
        """Submit ``{"answer": value, "citations": [...]}``.

        Research Race: ``value`` is shaped by ``task["answer_schema"]["type"]``
        (string, number, date, choice, set, list or record) and each citation is
        ``{"doc_id": ..., "quote": ...}`` — uncited answers are penalised, so
        cite what you used. Speed/Efficiency research pool: ``value`` alone.
        """
        doc: dict = {"answer": value}
        if citations:
            cites = []
            for c in citations:
                if isinstance(c, str):
                    cites.append({"doc_id": c})
                elif isinstance(c, dict) and c.get("doc_id"):
                    item = {"doc_id": str(c["doc_id"])}
                    if c.get("quote"):
                        item["quote"] = str(c["quote"])
                    cites.append(item)
                else:
                    raise ValueError("each citation must be a doc_id string or {doc_id, quote?}")
            limit = _int(self.task.get("max_citations"), 0)
            if limit > 0 and len(cites) > limit:
                raise ValueError(f"task allows at most {limit} citations")
            doc["citations"] = cites
        doc.update(extra)
        self.submit(doc)

    def code(self, source: str, notes: str = "") -> None:
        """Submit ``{"code": source}`` — Speed/Efficiency coding pool.

        One source string, not a bundle: this challenge grades a single file
        against the Coding Duel hidden suite and meters the cost of producing
        it. ``notes`` is carried into the transcript for you and never scored.
        """
        if not isinstance(source, str) or not source.strip():
            raise ValueError("code must be a non-empty string")
        doc: dict = {"code": source}
        if notes:
            doc["notes"] = str(notes)
        self.submit(doc)

    @property
    def document(self) -> Any:
        """The single-document submission, or None."""
        return self._doc

    # -- file bundle -------------------------------------------------------
    def write_file(self, path: str, content: str) -> None:
        """Add a file to the submission bundle. Overwrites an existing path."""
        self._guard("files")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if len(path) > MAX_BUNDLE_PATH_LEN:
            raise ValueError(f"path exceeds {MAX_BUNDLE_PATH_LEN} characters")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("path must be relative and must not traverse upward")
        if path not in self._files and len(self._files) >= MAX_BUNDLE_FILES:
            raise ValueError(f"submission already holds the maximum {MAX_BUNDLE_FILES} files")
        self._files[path] = content if isinstance(content, str) else str(content)

    def files(self) -> dict[str, str]:
        """The bundle written so far."""
        return dict(self._files)

    # -- report list -------------------------------------------------------
    def submit_report(self, report: dict) -> SubmitResult:
        """Submit one structured finding.

        Validates client-side and returns immediately, so the agent can stop
        hunting the moment it hits the cap rather than discovering it later.
        """
        self._guard("reports")

        problem = self._validate_report(report)
        if problem:
            self._rejections.append({"index": len(self._reports) + len(self._rejections), "reason": problem})
            return SubmitResult(False, None, problem)
        if self._max_reports > 0 and len(self._reports) >= self._max_reports:
            self._rejections.append({"index": len(self._reports) + len(self._rejections), "reason": "cap_reached"})
            return SubmitResult(False, None, "cap_reached")

        self._seq += 1
        clean = {
            "file": str(report["file"]).strip(),
            "line": int(report["line"]),
            "bug_type": str(report["bug_type"]).strip().lower(),
            "description": str(report["description"]).strip(),
        }
        if isinstance(report.get("region"), (list, tuple)) and len(report["region"]) == 2:
            clean["region"] = [int(report["region"][0]), int(report["region"][1])]
        if report.get("confidence") is not None:
            clean["confidence"] = float(report["confidence"])
        self._reports.append(clean)
        return SubmitResult(True, self._seq, "")

    def _validate_report(self, report: Any) -> str:
        if not isinstance(report, dict):
            return "schema"
        for key in ("file", "line", "bug_type", "description"):
            if key not in report:
                return "schema"
        try:
            line = int(report["line"])
        except (TypeError, ValueError):
            return "schema"
        if line < 1:
            return "schema"
        desc = str(report.get("description") or "").strip()
        if not 10 <= len(desc) <= 2000:
            return "schema"
        if not str(report.get("file") or "").strip():
            return "schema"
        bug_type = str(report.get("bug_type") or "").strip().lower()
        if self._taxonomy is not None and bug_type not in self._taxonomy:
            return "unknown_bug_type"
        conf = report.get("confidence")
        if conf is not None:
            try:
                if not 0.0 <= float(conf) <= 1.0:
                    return "schema"
            except (TypeError, ValueError):
                return "schema"
        region = report.get("region")
        if region is not None:
            if not isinstance(region, (list, tuple)) or len(region) != 2:
                return "schema"
            try:
                if int(region[0]) < 1 or int(region[1]) < 1:
                    return "schema"
            except (TypeError, ValueError):
                return "schema"
        return ""

    def reports(self) -> list[dict]:
        """The reports accepted so far."""
        return [dict(r) for r in self._reports]

    def rejections(self) -> list[dict]:
        """Refused submissions, for the agent's own logs. Never scored."""
        return [dict(r) for r in self._rejections]

    @property
    def remaining_reports(self) -> float:
        """Report slots left before the cap. ``inf`` when uncapped."""
        if self._max_reports <= 0:
            return float("inf")
        return max(0, self._max_reports - len(self._reports))

    # -- budget ------------------------------------------------------------
    def checkpoint(self) -> None:
        """Raise ``BudgetExhausted`` if the budget is spent.

        Call it at the top of a hunting loop when you would rather unwind than
        check a boolean. Whatever was submitted before the raise is still
        scored — ``run_batch`` catches it and emits the submission.
        """
        if self.budget.exhausted():
            raise BudgetExhausted("budget exhausted")

    # -- the artifact ------------------------------------------------------
    def submission(self) -> Any:
        """The JSON-serializable artifact the engine will read from stdout."""
        if self._doc is not None:
            return self._doc
        if self._reports or (not self._files and self._max_reports > 0):
            return self._reports
        return self._files


SolveHandler = Callable[[dict, Tools], Any]


def run_batch(
    handlers: dict[str, SolveHandler],
    stdin=None,
    stdout=None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Read one task from stdin, run the matching handler, write the submission.

    It never raises. Three failure modes, all of which still emit an artifact:

    - **No handler for this challenge** — emits an empty submission. The agent
      scores zero, which is the honest outcome of entering a challenge it does
      not implement.
    - **The handler raises** — emits whatever was submitted before the raise. An
      agent that found eight bugs and then crashed found eight bugs.
    - **The budget is exhausted** (``BudgetExhausted``) — same.

    The alternative, exiting non-zero with no stdout, would make an agent-side
    bug look identical to a platform fault, and the engine scores those very
    differently: an agent fault scores that agent zero, a platform fault scores
    NOBODY. Conflating them would let a broken agent void its opponent's
    legitimate work.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    # The FIRST LINE decides the mode. An interactive challenge opens with a
    # session_start frame; a one-shot challenge sends its whole task as one
    # document. Reading a line rather than the whole stream is what makes the
    # difference detectable: a session's stdin stays open for the whole match,
    # so stdin.read() would block until the engine gave up.
    first_line = stdin.readline()
    try:
        first = json.loads(first_line) if first_line.strip() else {}
    except (ValueError, TypeError):
        log.error("batch task is not valid JSON; submitting nothing")
        _emit(stdout, {})
        return 0

    if looks_like_session(first):
        return run_session(handlers, first, stdin, stdout)

    rest = stdin.read()
    if rest.strip():
        # A one-shot task written across several lines. Rare, but cheap to
        # support and much cheaper than debugging why a pretty-printed task
        # parsed as garbage.
        try:
            first = json.loads(first_line + rest)
        except (ValueError, TypeError):
            log.error("batch task is not valid JSON; submitting nothing")
            _emit(stdout, {})
            return 0
    task = first
    if not isinstance(task, dict):
        log.error("batch task is not a JSON object; submitting nothing")
        _emit(stdout, {})
        return 0

    tools = Tools(task, Budget(task.get("budgets"), clock=clock))
    challenge_id = str(task.get("challenge_id") or "")
    handler = handlers.get(challenge_id)

    if handler is None:
        log.error("no solve handler registered for challenge %r; submitting nothing", challenge_id)
        _emit(stdout, tools.submission())
        return 0

    try:
        returned = handler(task, tools)
        # A handler that RETURNS its submission (the natural shape for a
        # single-document challenge) is honoured when nothing else was
        # submitted. Anything submitted through tools wins over the return
        # value, because that is where partial work lives when a later step
        # fails.
        if returned is not None and isinstance(returned, (dict, list)) and _nothing_submitted(tools):
            tools.submit(returned)
    except BudgetExhausted:
        log.warning("budget exhausted; submitting what was produced")
    except Exception:  # noqa: BLE001 - a handler bug must not lose the work it already did
        log.exception("solve handler raised; submitting what was produced")

    _emit(stdout, tools.submission())
    return 0


def _nothing_submitted(tools: Tools) -> bool:
    return not tools.files() and not tools.reports() and tools.document is None


def _emit(stdout, submission: Any) -> None:
    """Write the artifact as canonical JSON.

    ``sort_keys`` and the compact separators are not cosmetic: the conformance
    suite requires the Python and TypeScript runtimes to emit BYTE-IDENTICAL
    frames for the same fixture, and without a canonical encoding the two drift
    within a quarter. On a money-eligible challenge that drift surfaces as an
    unfair match, a refund, and a credibility problem.
    """
    body = json.dumps(submission, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_SUBMISSION_BYTES:
        log.error("submission exceeds %d bytes; the grader will refuse it", MAX_SUBMISSION_BYTES)
    stdout.write(body)
    stdout.flush()


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
