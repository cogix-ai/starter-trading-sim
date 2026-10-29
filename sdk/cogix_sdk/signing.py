"""Ed25519 per-move signing — the ``arena-move-v1`` domain.

Byte-identical to backend/internal/movesig/movesig.go, proven by the golden
vectors in sdk/contract/vectors/movesig.json. A signature binds one move to
exactly one (match, round, seat, card) slot — it cannot be replayed elsewhere.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

DOMAIN = "arena-move-v1"


def message(match_id: str, round_: int, seat: int, card: int) -> bytes:
    """Canonical bytes to sign: domain\\nmatch\\nround\\nseat\\ncard."""
    return "\n".join([DOMAIN, match_id, str(round_), str(seat), str(card)]).encode()


def keypair_from_seed(seed: bytes) -> tuple[Ed25519PrivateKey, str]:
    """Load a private key from a 32-byte seed; returns (key, base64 public key)."""
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes_raw()
    return priv, base64.b64encode(pub).decode()


def sign(priv: Ed25519PrivateKey, match_id: str, round_: int, seat: int, card: int) -> str:
    """Sign one move slot; returns the base64 signature."""
    return base64.b64encode(priv.sign(message(match_id, round_, seat, card))).decode()


def verify(pub_b64: str, match_id: str, round_: int, seat: int, card: int, sig_b64: str) -> bool:
    """Verify a base64 signature against a base64 public key. Any error -> False."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pub.verify(base64.b64decode(sig_b64), message(match_id, round_, seat, card))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Build signing — the ``agentbuild.v1`` domain
# ---------------------------------------------------------------------------
#
# Byte-identical to backend/internal/agentbuild/signature.go, proven by the
# golden vectors in sdk/contract/vectors/agentbuildsig.json.
#
# A build is the container image an agent competes from, and the platform
# refuses to admit one that is not signed by a key the developer registered. The
# scheme previously existed only in Go — SignBuild's own comment says it "exists
# for the SDK", and the SDK did not have it — so a Python developer could not
# submit a build at all, and an agent with no admitted build cannot enter any AI
# challenge.

BUILD_DOMAIN = "agentbuild.v1"


def _field(s: str) -> str:
    """One length-prefixed field: ``<len>:<bytes>``.

    The length is in BYTES, not characters. A non-ASCII argv is where a
    character-counting implementation diverges — and nowhere else, which is why
    the vectors carry one.
    """
    return f"{len(s.encode())}:{s}"


def build_message(
    agent_public_id: str,
    digest: str,
    entrypoint: list[str],
    entrypoints: list[str],
    sdk_version: str,
) -> bytes:
    """Canonical bytes to sign for a build submission.

    ``entrypoint`` is the container argv. ``entrypoints`` are the SDK roles the
    build declares (solve, ctf_attack, …). They are signed sorted and
    de-duplicated, because that is the form the platform verifies — so the
    order you list them in does not matter.
    """
    entrypoints = sorted(set(entrypoints))
    parts = [BUILD_DOMAIN + "\n", _field(agent_public_id), _field(digest), _field(str(len(entrypoint)))]
    parts += [_field(a) for a in entrypoint]
    parts.append(_field(str(len(entrypoints))))
    parts += [_field(e) for e in entrypoints]
    parts.append(_field(sdk_version))
    return "".join(parts).encode()


def sign_build(
    seed: bytes,
    agent_public_id: str,
    digest: str,
    entrypoint: list[str],
    entrypoints: list[str],
    sdk_version: str,
) -> str:
    """Sign a build submission; returns the base64 signature the API expects.

    ``seed`` is the 32-byte Ed25519 private seed whose public half was
    registered at ``PUT /v1/agents/{agent_id}/signing-key``.
    """
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    msg = build_message(agent_public_id, digest, entrypoint, entrypoints, sdk_version)
    return base64.b64encode(priv.sign(msg)).decode()
