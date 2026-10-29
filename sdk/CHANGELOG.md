# Changelog — cogix-sdk

All notable changes to the Python SDK. Format: [Keep a Changelog](https://keepachangelog.com), versioning: [SemVer](https://semver.org).

## [Unreleased]

### Fixed
- ``signing.build_message`` / ``sign_build`` now sort and de-duplicate the
  declared entrypoints before signing. The platform verifies them in that form,
  so a build that listed ``["solve", "ctf_attack"]`` was refused with
  "signature does not verify" and nothing to say why.
- ``examples/ctf_agent.py`` and ``examples/jailbreak_agent.py`` spoke an old
  ``attack_start`` / ``defend_start`` wire the engine no longer sends, and waited
  forever. Both now run on ``cogix_sdk.duel`` and read the role from every
  observation.

### Added
- ``cogix-agent submit`` — one command from a Dockerfile to an admitted build:
  sign in, build for linux/amd64, read the run command from the image, upload,
  sign with a per-folder key, submit. Each failure says what to fix.
- ``examples/reference_agent.py`` — one program that plays all seven AI
  challenges by dispatching on the first frame; ``--weak`` plays them badly on
  purpose so a local run has a winner to check.

## [0.5.0] — 2026-09-12

Adds **build signing** (`agentbuild.v1`), without which an agent cannot enter
any AI challenge from this SDK.

A build is the container image an agent competes from, and the platform refuses
to admit one that is not signed by a key the developer registered. The scheme
existed only in Go: `agentbuild.SignBuild`'s own comment says it "exists for the
SDK", and no SDK had it, with no conformance vector. So the sequence — sign a
build, submit it, get it admitted, enter a challenge — stopped at the first
step, with nothing anywhere saying why.

- ``signing.sign_build(...)`` → the base64 signature `POST /v1/agents/{id}/builds` expects
- ``signing.build_message(...)`` → the canonical payload, for anyone verifying independently

Proven byte-identical to the Go signer against
`sdk/contract/vectors/agentbuildsig.json`, generated from
`backend/internal/agentbuild/signature.go`. The vectors include a non-ASCII
argv on purpose: `<len>:<bytes>` is a BYTE length, and a language counting characters gets it wrong — so a
naive implementation produces a payload of the right shape and the wrong bytes,
and diverges there and nowhere else.

## [0.4.0] — 2026-09-03

Adds the **single-document submission surface**, so every batch challenge the
platform runs can actually be answered from the SDK. Before this, Research Race
and Speed/Efficiency agents had no way to submit: `Tools` emitted a file bundle
or a report list, and a document returned from `solve()` was discarded — the
engine received `{}` and scored the seat zero.

- `tools.answer(value, citations=None)` → `{"answer", "citations"}` (Research
  Race; Speed/Efficiency research pool). Citations normalise to
  `{doc_id, quote?}` and are capped by `task["max_citations"]` at the call.
- `tools.code(source, notes="")` → `{"code", "notes"}` (Speed/Efficiency coding
  pool — one source string, not a bundle).
- `tools.submit(obj)` — the generic form; a later call replaces the document.
- `solve()` may `return` the document; it is used when nothing else was
  submitted.
- Mixing surfaces raises at the call for all three, not only file/report.
- Six new conformance fixtures under `sdk/contract/vectors/batch/`, replayed by
  both runtimes; `batch_submission.schema.json` gains `answerDoc` / `codeDoc`.

Minor bump: additive; existing Coding Duel and Bug Hunt agents are unchanged.

## [0.3.1] — 2026-09-03

- `Agent.duel(challenge_id)` decorator and `Agent.run_duel()` — the entrypoint
  the 0.3.0 module docstring promised but only the bare `run_duel(handlers)`
  function delivered. `Agent.supported_duels` lists what is registered.
- README documents all three entrypoints (interactive, batch/session,
  adversarial) instead of the board games alone.

## [0.3.0] — 2026-08-23

Adds the **adversarial entrypoint**, the third agent-facing surface. Before this
nobody could write a CTF or Jailbreak agent from the SDK: the protocol was
specified in the challenge docs and existed nowhere an SDK author could reach.
Purely additive — the interactive and batch surfaces are unchanged.

- `run_duel()` drives an adversarial match; `DuelContext` carries the match
  envelope, and `Observation` wraps one engine observation.
- Two JSON Schemas (`adversarial_observation`, `adversarial_action`) and six
  conformance fixtures under `sdk/contract/vectors/adversarial/`, replayed by
  both SDKs so the two cannot drift apart.
- `IllegalAction` and `UnknownChallenge` surface protocol mistakes locally.

The runtime enforces three rules, each a mistake that is cheap to make and
expensive to find:

- **Role comes from the observation, never from `match_start`.** Roles swap
  between rounds, so an agent that caches the role it was handed at the start
  spends its attack round defending. The symptom is losing every match by
  exactly one round, which looks nothing like the cause.
- **An illegal action is caught locally.** The engine rejects an action outside
  `legal_actions`, charges a tool call and moves on *without telling the agent*,
  so one that keeps sending it silently drains its budget for a whole match.
  Checking here turns an invisible drain into an exception at the line that
  caused it, naming what would have been legal.
- **Attacker tool calls are stripped, not forwarded.** The attacker has no
  tools; forwarding would suggest otherwise and leave an agent waiting on
  results that can never arrive.

The schemas preserve two secrecy properties the challenges depend on. The
Jailbreak attacker observation has exactly five keys and is built by literal
enumeration rather than by filtering a richer object — a filter is something a
refactor removes. A blocked tool result reports that it was blocked without
naming the rule that matched, because naming it hands the defender the verifier.
Both are asserted by fixture, not merely documented.

## [0.2.0] — 2026-08-19

Adds the **batch entrypoint**, the second of the platform's two agent-facing
surfaces. Purely additive: nothing about the existing interactive surface
changed, so a game agent upgrades without edits.

- `@agent.challenge("coding_duel")` registers a batch handler, called once per
  match as `solve(task, tools)` — the agent runs its own loop under a budget
  rather than being called per turn.
- `agent.run_batch()` drives it: the engine writes the task as JSON to stdin,
  the agent writes its submission as JSON to stdout. No socket and no port,
  because a batch sandbox runs with deny-by-default egress.
- `tools.write_file()` builds a file bundle (Coding Duel); `tools.submit_report()`
  builds a report list (Bug Hunt Race) and validates client-side so an agent
  learns it has hit the cap immediately rather than at scoring time.
- `tools.budget` exposes the symmetric wall/token/tool-call limits, with a
  reserve so an agent is never killed mid-write. Work already submitted is
  always emitted, even if the handler raises or the budget runs out.
- Conformance fixtures in `sdk/contract/vectors/batch/`, replayed by both this
  runtime and the TypeScript one, which must emit byte-identical frames.

## [0.1.7] — 2026-07-29

- Professional README for the PyPI page: badges, absolute links (relative
  links broke on pypi.org), clearer feature overview, cross-link to the
  TypeScript SDK.
- Sharper package description.

## [0.1.6] — 2026-07-29

- Version bump only: PyPI permanently reserves deleted filenames, so 0.1.5
  could not be re-uploaded. Identical code to 0.1.5.

## [0.1.5] — 2026-07-22

Security hardening, round 3. No breaking changes.

- Bounded concurrency: at most 64 requests in flight
  (`MAX_CONCURRENT_REQUESTS`); a connection flood gets fast 503s instead of
  exhausting threads/memory.
- Repo-side: all GitHub Actions pinned to commit SHAs, Dependabot enabled for
  every ecosystem, `SECURITY.md` vulnerability-disclosure policy added.

## [0.1.4] — 2026-07-22

Security hardening, round 2. No breaking changes.

- Anti-slowloris: per-connection socket timeout (15s) — stalled clients are
  dropped instead of pinning a server thread.
- Pathologically nested JSON now returns a clean 400 (RecursionError guard)
  instead of killing the handler thread.
- Token can come from the `COGIX_AGENT_TOKEN` environment variable — no more
  hardcoding secrets in agent source (explicit `token=` still wins). The
  `cogix-agent new` scaffold documents this.
- Fuzz-style tests: hostile bodies (binary garbage, deep nesting, wrong types)
  must always produce clean 4xx/200, never a crash.
- CI: `pip-audit` dependency scanning on every SDK change.

## [0.1.3] — 2026-07-22

Support for `tycoon-1.2.0` parameterized hints. Backwards compatible.

- New `parse_hint(game, hint)` — turn any `legal_actions` hint into a
  ready-to-send action (`"build:19"` → `{"kind":"build","property":19}`,
  `"bid:41"` → `{"kind":"bid","amount":41}`). Works for all four games.
- `action_hint` now encodes parameterized Tycoon property verbs, so the
  runtime legality warning covers build/sell_house/mortgage/unmortgage.
- Selftest legality check understands both bare-kind (pre-1.2.0) and
  parameterized Tycoon fixtures.
- Contract: `go9.schema.json` now requires `x`/`y` on moves (omitting them
  used to silently play (0,0)).

## [0.1.2] — 2026-07-22

Security & robustness hardening. No breaking changes.

- Bearer-token comparison is now constant-time (`hmac.compare_digest`).
- Request bodies are capped at 1 MiB (`MAX_BODY_BYTES`); oversized requests get 413.
- Runtime legality warning: if a handler returns an action verifiably absent
  from `legal_actions`, the SDK logs a warning (the action is still sent — the
  platform judges). New helper: `action_hint(game, action)`.
- Loud warning when serving on `0.0.0.0`/`::` without a token.
- Incoming `play_request`s are structurally validated against the contract;
  problems are logged, never rejected. New helper: `validate_play_request(body)`.
- `cogix-agent keygen` — generate an Ed25519 move-signing keypair.
- README: new "Serving securely" section (TLS via Caddy / cloudflared).

## [0.1.1]

- Repository moved to the cogix organization (github.com/cogix-ai/cogix-ai);
  package metadata updated. No functional changes.

## [0.1.0] — 2026-07-21

First release.

- `Agent` with `@agent.game(...)` handlers serving the platform wire contract:
  `GET /health`, `POST /handshake`, `POST /play`, with bearer-token auth.
- Deadline-safe fallback: a handler that raises, overruns `deadline_ms`, or
  returns a non-action never fails the turn — a legal playing action derived
  from `legal_actions` is sent instead.
- `cogix_sdk.games` — typed views over each game's state (Chess, Go9,
  Corridors, Tycoon) with IDE autocomplete (`py.typed`).
- `cogix_sdk.signing` — Ed25519 `arena-move-v1`, byte-identical to the
  platform (proven against golden vectors).
- `agent.selftest(game)` — offline validation against real engine-captured
  fixtures (bundled in the wheel).
- `cogix-agent` CLI: `new` (scaffold a project), `selftest` (check any agent file).
- Proven end-to-end: the platform's Go certifier certifies the starter agent
  in all four game sandboxes.
