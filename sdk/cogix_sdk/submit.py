"""``cogix-agent submit``: build this folder's agent and enter it on Cogix.

One command for what used to be six manual steps, each of which failed in its
own quiet way:

  1. sign in (first, so a wrong password costs a second, not a build).
  2. docker build, for linux/amd64 — the arena's architecture, not this
     laptop's. An arm64 image from an Apple Silicon machine uploads fine and
     dies in the sandbox with an exec format error that reads as "no submission".
  3. read the run command from the image (its ENTRYPOINT + CMD), so nobody types
     an argv by hand. ``docker export`` drops the image config, so it is read
     before exporting and sent alongside.
  4. docker export the filesystem to a tar, and register this folder's
     signing key (created on first run, kept in .cogix/signing-key, never sent).
  5. upload the tar gzip-compressed (Cloudflare refuses bodies over 100 MB);
     the platform digests the decompressed tar.
  6. sign the build and submit it, then report whether it was admitted.

Settings come from the environment, then a prompt:

    COGIX_API        default https://cogix.cc/api
    COGIX_EMAIL      your Cogix account (remembered in .cogix/settings.json)
    COGIX_PASSWORD   read from the environment or prompted; never stored
    COGIX_AGENT_ID   optional: by default your agent is looked up after
                     sign-in, and remembered for this folder
"""

from __future__ import annotations

import base64
import getpass
import gzip
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Callable

from . import __version__, signing

DEFAULT_API = "https://cogix.cc/api"
PLATFORM = "linux/amd64"
KEY_FILE = pathlib.Path(".cogix") / "signing-key"
SETTINGS_FILE = pathlib.Path(".cogix") / "settings.json"
USER_AGENT = f"cogix-agent/{__version__} (+https://cogix.cc)"
# Cloudflare in front of cogix.cc refuses request bodies over 100 MB.
MAX_UPLOAD_BYTES = 100_000_000


class SubmitError(Exception):
    """A step failed; the message says which and what to do about it."""


# --- pure pieces ----------------------------------------------------------


def entrypoint_from_config(config: dict) -> list[str]:
    """The argv a container from this image runs: ENTRYPOINT, then CMD.

    The sandbox has no shell and no PATH lookup it can rely on, so the program
    must be an absolute path inside the image.
    """
    argv = list(config.get("Entrypoint") or []) + list(config.get("Cmd") or [])
    if not argv:
        raise SubmitError(
            "The Dockerfile has no ENTRYPOINT or CMD, so there is nothing to run.\n"
            '  Add one, e.g. ENTRYPOINT ["/usr/local/bin/python3", "/agent/agent.py"]')
    if argv[:2] in (["/bin/sh", "-c"], ["sh", "-c"]):
        raise SubmitError(
            "The Dockerfile uses the shell form of ENTRYPOINT/CMD. Use the JSON form:\n"
            '  ENTRYPOINT ["/usr/local/bin/python3", "/agent/agent.py"]')
    if not argv[0].startswith("/"):
        raise SubmitError(
            f"The run command starts with {argv[0]!r}, which is not an absolute path.\n"
            "  The sandbox does not search PATH. Use the full path, e.g. /usr/local/bin/python3")
    return argv


def load_or_create_key(path: pathlib.Path = KEY_FILE) -> bytes:
    """This folder's 32-byte signing seed, created on first use."""
    if path.exists():
        seed = base64.b64decode(path.read_text().strip())
        if len(seed) != 32:
            raise SubmitError(f"{path} is not a valid signing key. Delete it and run again.")
        return seed
    seed = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(seed).decode() + "\n")
    path.chmod(0o600)
    return seed


def setting(name: str, prompt: str, *, secret: bool = False,
            ask: Callable[[str], str] | None = None) -> str:
    """An environment variable, or the answer to a prompt."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not sys.stdin.isatty() and ask is None:
        raise SubmitError(f"Set {name} (no terminal to ask on).")
    ask = ask or (getpass.getpass if secret else input)
    value = ask(prompt).strip()
    if not value:
        raise SubmitError(f"{name} is required.")
    return value


def load_settings(path: pathlib.Path = SETTINGS_FILE) -> dict:
    """What this folder remembers between runs: the email and the agent. Never
    the password."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(values: dict, path: pathlib.Path = SETTINGS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n")


def choose_agent(rows: list, remembered: str = "",
                 ask: Callable[[str], str] | None = None) -> dict:
    """Pick the agent a build is for, from the account's own list.

    The one remembered for this folder wins; a single agent needs no question;
    several get a numbered list. Nobody has to find an id on a web page.
    """
    live = [r for r in rows if r.get("status") != "retired"]
    if not live:
        raise SubmitError(
            "This account has no agent yet. Create one at https://cogix.cc/agents/new "
            "(it only needs a name), then run ./submit again.")
    for r in live:
        if r.get("agent_id") == remembered:
            return r
    if len(live) == 1:
        return live[0]
    if ask is None:
        if not sys.stdin.isatty():
            raise SubmitError("This account has several agents. Set COGIX_AGENT_ID to choose one.")
        ask = input
    lines = "\n".join(f"  {i}. {r.get('name')}  ({r.get('agent_id')})" for i, r in enumerate(live, 1))
    pick = ask(f"Which agent is this build for?\n{lines}\nNumber: ").strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(live):
        raise SubmitError(f"Pick a number from 1 to {len(live)}.")
    return live[int(pick) - 1]


# --- the platform ---------------------------------------------------------


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token = ""

    def call(self, method: str, path: str, body: object = None, *,
             data: object = None, headers: dict | None = None, timeout: int = 60) -> dict:
        req = urllib.request.Request(self.base + path, method=method)
        # Say who we are. Cloudflare in front of cogix.cc refuses the default
        # "Python-urllib" signature with error 1010 before Cogix ever sees it.
        req.add_header("User-Agent", USER_AGENT)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        elif data is not None:
            req.data = data  # type: ignore[assignment]
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise SubmitError(_explain(e.code, e.read())) from None
        except urllib.error.URLError as e:
            raise SubmitError(f"Could not reach {self.base}: {e.reason}") from None


def _explain(status: int, raw: bytes) -> str:
    if raw.strip().startswith(b"error code:"):
        # Cloudflare's own block page, not a Cogix answer.
        return (f"The request was blocked before reaching Cogix ({raw.strip().decode(errors='replace')}).\n"
                "  Update the starter (git pull) and try again; if it persists, tell the Cogix team.")
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    # A rejected build comes back as its own record (422), not an error envelope.
    if body.get("status") == "rejected":
        why = body.get("reason") or body.get("scan_detail") or "no reason given"
        return f"The build was rejected: {why}"
    err = body.get("error") or {}
    msg = err.get("message") or raw.decode(errors="replace")[:300]
    if status == 401:
        return "Sign-in failed: check COGIX_EMAIL and COGIX_PASSWORD."
    if status == 403:
        return f"Not allowed: {msg}\n  Is COGIX_AGENT_ID one of your agents?"
    return f"{status}: {msg}"


# --- docker ---------------------------------------------------------------


def _docker(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


def build_image(context: pathlib.Path, tag: str) -> list[str]:
    """Build for the arena's platform and return the image's run command."""
    if not shutil.which("docker"):
        raise SubmitError("Docker is not installed. Install Docker Desktop and run again.")
    if _docker(["info"], timeout=60).returncode != 0:
        raise SubmitError("Docker is installed but not running. Start Docker Desktop and run again.")
    if not (context / "Dockerfile").exists():
        raise SubmitError(f"No Dockerfile in {context.resolve()}. Run this from your agent's folder.")
    out = _docker(["build", "--platform", PLATFORM, "-q", "-t", tag, str(context)], timeout=1800)
    if out.returncode != 0:
        raise SubmitError("docker build failed:\n" + out.stderr.strip()[-1500:])
    cfg = _docker(["image", "inspect", "--format", "{{json .Config}}", tag], timeout=60)
    if cfg.returncode != 0:
        raise SubmitError("Could not read the built image: " + cfg.stderr.strip()[:300])
    return entrypoint_from_config(json.loads(cfg.stdout))


def export_image(tag: str, dest: pathlib.Path) -> None:
    made = _docker(["create", "--platform", PLATFORM, tag], timeout=120)
    if made.returncode != 0:
        raise SubmitError("docker create failed: " + made.stderr.strip()[:300])
    cid = made.stdout.strip()
    try:
        with dest.open("wb") as f:
            exp = subprocess.run(["docker", "export", cid], stdout=f, stderr=subprocess.PIPE, timeout=900)
        if exp.returncode != 0:
            raise SubmitError("docker export failed: " + exp.stderr.decode()[:300])
    finally:
        _docker(["rm", "-f", cid], timeout=120)


def compress(src: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """gzip the exported tar. The platform stores and digests the DECOMPRESSED
    bytes, so this only changes what crosses the network."""
    with src.open("rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    return dest


# --- the command ----------------------------------------------------------


def _say(line: str) -> None:
    print(line, flush=True)  # progress must land before an error on stderr


def submit(context: pathlib.Path, entrypoints: list[str], say: Callable[[str], None] = _say) -> dict:
    api = Api(os.environ.get("COGIX_API", "").strip() or DEFAULT_API)
    remembered = load_settings()
    email = os.environ.get("COGIX_EMAIL", "").strip() or remembered.get("email", "")
    if email:
        say(f"Signing in as {email}")
    else:
        email = setting("COGIX_EMAIL", "Cogix email: ")
    password = setting("COGIX_PASSWORD", "Cogix password: ", secret=True)

    # Sign in first: a wrong password should cost a second, not a build.
    say("1/5 Signing in…")
    login = api.call("POST", "/v1/auth/login", {"email": email, "password": password})
    api.token = login.get("dashboard_token") or ""
    if not api.token:
        raise SubmitError("Signed in, but the platform returned no session token.")

    agent = os.environ.get("COGIX_AGENT_ID", "").strip()
    if not agent:
        rows = api.call("GET", "/v1/agents").get("agents") or []
        chosen = choose_agent(rows, remembered.get("agent_id", ""))
        agent = chosen["agent_id"]
        say(f"    agent: {chosen.get('name')} ({agent})")
    save_settings({"email": email, "agent_id": agent})

    say("2/5 Building your agent (linux/amd64)…")
    tag = f"cogix-submit/{agent.lower()}:latest"
    entrypoint = build_image(context, tag)
    say("    runs: " + " ".join(entrypoint))

    say("3/5 Registering this folder's signing key…")
    seed = load_or_create_key()
    _, pub = signing.keypair_from_seed(seed)
    api.call("PUT", f"/v1/agents/{agent}/signing-key", {"public_key": pub})

    say("4/5 Uploading the image…")
    work = pathlib.Path(tempfile.mkdtemp(prefix="cogix-submit-"))
    try:
        tar = work / "rootfs.tar"
        export_image(tag, tar)
        raw = tar.stat().st_size
        packed = compress(tar, work / "rootfs.tar.gz")
        size = packed.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise SubmitError(
                f"Your image is {size / 1e6:.0f} MB even compressed; the upload limit is "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB.\n"
                "  Remove large dependencies (big ML libraries, data files) from the Dockerfile's image.")
        with packed.open("rb") as body:
            up = api.call("POST", f"/v1/agents/{agent}/builds/upload", data=body, timeout=900,
                          headers={"Content-Type": "application/octet-stream",
                                   "Content-Encoding": "gzip",
                                   "Content-Length": str(size)})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    digest = up.get("digest") or ""
    if not digest:
        raise SubmitError("The upload finished but the platform returned no digest.")
    say(f"    {raw / 1e6:.0f} MB image, {size / 1e6:.0f} MB uploaded, {digest[:19]}…")

    say("5/5 Submitting the signed build…")
    sig = signing.sign_build(seed, agent, digest, entrypoint, entrypoints, __version__)
    build = api.call("POST", f"/v1/agents/{agent}/builds", {
        "ref": f"cogix-submit/{context.resolve().name}", "digest": digest, "entrypoint": entrypoint, "entrypoints": entrypoints,
        "sdk_version": __version__, "signature": sig})
    status = build.get("status")
    if status != "admitted":
        why = build.get("reason") or build.get("scan_detail") or ""
        raise SubmitError(f"The build was not admitted (status: {status}). {why}".strip())
    return build


def cmd_submit(args) -> int:
    try:
        build = submit(pathlib.Path(args.dir), args.entrypoints.split(","))
    except SubmitError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    print(f"\n✓ Build {build.get('id', '')} admitted. Your agent can play now:")
    print("  open your agent's page on cogix.cc and press “Find an opponent”.")
    return 0
