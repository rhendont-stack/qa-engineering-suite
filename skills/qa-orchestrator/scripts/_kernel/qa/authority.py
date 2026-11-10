"""Machine-bound authority (§12.10): a recorded approval counts only on the machine that recorded it.

Every event that carries or changes an approval holds ``data.authority_mac``, an HMAC-SHA256 over its type and its stored
data, made with ``.qa/authority.key``. That key is 32 random bytes, created by ``qa init`` and, in a workspace older than
this module, on the first signature; it is never replaced. It has the same standing as ``redaction.key``: owner-only, git
ignores it, and neither a bundle nor a container archive carries it.

A MAC is computed by ``ledger.append`` after redaction, so it verifies what the ledger stores, and it leaves its own field
out of the message. ``state.load`` folds the events that confer authority (``CONFERRING``) only when their MAC verifies,
so a ledger copied from another machine or written before this module has no accepted configuration and no grants. A
revocation is signed too, but it counts without a MAC: it only takes authority away. Old events still verify in the hash
chains; they are only powerless, and the way back is to approve again here (``RECOVERY``).

What this does not do (``LIMIT`` restates the first point in ``qa doctor``):

- The key lives in ``.qa/`` and nothing tells a key created here from one that arrived with the directory. A whole-directory
  transfer outside git (an archive, ``rsync``, ``cp -a``) that brings the key along, including an archive someone prepared
  with a key of their own and events signed by it, verifies, so its grants and accepted configuration take effect
  (§12.10 point 5). The key stops a ledger copied without it; ``tracked_authority`` covers only the git route.
- Anyone who can read the key on this machine can sign, and a signed event can be replayed in the same workspace.
- Anyone who can write the ledger can delete a revocation and re-hash the chain, because the chains are unkeyed.
- ``finding.state_changed`` to ``accepted_risk`` or ``wont_fix`` is not signed, so those decisions in a copied ledger
  still count.
- A MAC does not prove that a person gave the approval (§12.10 point 2).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Callable, Iterable, Optional, Tuple

from .canonical import dumps
from .errors import QaError
from .workspace import AUTHORITY_KEY_FILE, Workspace, ensure_authority_key, read_limited

# QaError codes raised here: authority_key_invalid.

MAC_FIELD = "authority_mac"
EVENTS = frozenset({"workspace.initialized", "config.accepted", "config.changed", "gate.policy_accepted",
                    "authorization.granted", "authorization.revoked", "requirement.confirmed", "assumption.decided",
                    "file.modification_approved"})
CONFERRING = EVENTS - {"authorization.revoked"}
RECOVERY = ('run `qa config accept --approval "<quote>"` to approve .qa/config.json on this machine, and `qa authorize` '
            "to record each grant again")
LIMIT = ("a .qa/ directory transferred whole outside git (archive, rsync, cp -a) brings its key along, and its approvals "
         "then verify here; a key that arrived with the directory cannot be told from one created here")

_KEY_BYTES = 32
_DOMAIN = b"qa-authority/1\0"
_PREFIX = "hmac-sha256:"


def _read_key(ws: Workspace) -> Tuple[Optional[bytes], str]:
    """The key and ``"ok"``, or None and ``"missing"`` or ``"invalid"``. A symlink, like any other non-regular file, is invalid."""
    try:
        key = read_limited(ws.path(AUTHORITY_KEY_FILE), _KEY_BYTES)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "invalid"
    return (key, "ok") if len(key) == _KEY_BYTES else (None, "invalid")


def key_status(ws: Workspace) -> str:
    """``ok``, ``missing`` or ``invalid``; never creates the key."""
    return _read_key(ws)[1]


def _mac(key: bytes, event_type: str, data: dict) -> str:
    body = {name: value for name, value in data.items() if name != MAC_FIELD}
    message = _DOMAIN + event_type.encode("utf-8") + b"\0" + dumps(body)
    return _PREFIX + hmac.new(key, message, hashlib.sha256).hexdigest()


def sign(ws: Workspace, event_type: str, data: dict) -> str:
    """The MAC of ``data`` as stored, creating the key if this workspace has none. An unusable key is never replaced."""
    ensure_authority_key(ws)
    key, status = _read_key(ws)
    if key is None:
        raise QaError("authority_key_invalid", ".qa/authority.key is not a readable file of %d bytes; nothing can be approved "
                      "until it is: delete it and approve again" % _KEY_BYTES, status=status)
    return _mac(key, event_type, data)


def _matches(key: bytes, event_type: str, data: object) -> bool:
    mac = data.get(MAC_FIELD) if isinstance(data, dict) else None
    if not isinstance(mac, str):
        return False
    try:
        expected = _mac(key, event_type, data)  # type: ignore[arg-type]
    except (TypeError, ValueError, RecursionError):  # a stored value canonical JSON cannot write
        return False
    return hmac.compare_digest(expected.encode("utf-8"), mac.encode("utf-8", "backslashreplace"))


def verify(ws: Workspace, event_type: str, data: dict) -> bool:
    """Whether this machine's key made ``data``'s MAC. Never raises; a missing or unusable key verifies nothing."""
    key = _read_key(ws)[0]
    return key is not None and _matches(key, event_type, data)


def event_verifier(ws: Workspace) -> Callable[[dict], bool]:
    """``verify`` for whole events, reading the key once: what ``state.load`` hands to the fold."""
    key = _read_key(ws)[0]

    def backed(event: dict) -> bool:
        return key is not None and _matches(key, event["type"], event["data"])

    return backed


def count_without_authority(ws: Workspace, events: Iterable[dict]) -> int:
    """How many of ``events`` would confer authority but do not verify here: what ``qa doctor`` reports."""
    backed = event_verifier(ws)
    return sum(1 for event in events if event["type"] in CONFERRING and not backed(event))
