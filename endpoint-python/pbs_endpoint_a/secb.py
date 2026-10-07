"""PBS-SEC-B-01 authenticated commands, PBS-AUTH-01 Authority Context and
the rover's receive procedure (SPEC-DIGEST.md Sections 5 and 6).

PBS_LINK 0.1.4 implements none of PBS-MUX-01, PBS-AUTH-01 or PBS-SEC-B-01;
this module implements them from the specification digest. HMAC-SHA-256
uses the Python standard library; Ed25519 uses the ``cryptography`` package.

All keys handled here are TEST KEYS derived from public labels. They are
not for operational use.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                               Ed25519PublicKey)

from . import envelope as E
from .mux import (FT_AUTH, FT_COMMAND, FT_EVENT, FT_SEC, FT_STATUS, FT_TELEMETRY,
                  MuxError, build_container, encode_frame, parse_container)

SUITE_HMAC = 0xFF01            # PBS-DEMO-TEST-HMAC-SHA256 (unregistered, A16)
SUITE_ED25519 = 0xFF02         # PBS-DEMO-TEST-ED25519 (unregistered, A16)
SUITE_AUTH_LEN = {SUITE_HMAC: 32, SUITE_ED25519: 64}
SUITE_NAMES = {SUITE_HMAC: "PBS-DEMO-TEST-HMAC-SHA256", SUITE_ED25519: "PBS-DEMO-TEST-ED25519"}
SEC_VERSION = 1
SEC_FLAG_AUTH = 0x01           # bit 0 = authentication (A17)
SEC_META_LEN = 16              # fixed part of the Security frame value
AUTH_VERSION = 1
AUTH_FIXED_LEN = 8
REPLAY_WINDOW = 64             # A22

TEST_KEY_LABEL = "PBS-ION-DEMO TEST KEY %s (NOT FOR OPERATIONAL USE)"


def suite_str(suite):
    return "0x%04X" % suite


def parse_suite(text):
    return int(text, 16) if isinstance(text, str) else int(text)


def derive_test_key(key_id):
    """32 bytes = SHA-256 of the public label. TEST ONLY (SPEC-DIGEST 6.3)."""
    return hashlib.sha256((TEST_KEY_LABEL % key_id).encode("ascii")).digest()


def ed25519_public(seed):
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

@dataclass
class VerifyKey:
    """Key material held by a receiver: Ed25519 public key or HMAC secret."""
    key_id: bytes
    suite: int
    material: bytes
    bound_source_id: str
    bound_authority: bytes

    def verify(self, message, tag):
        if self.suite == SUITE_HMAC:
            want = hmac.new(self.material, message, hashlib.sha256).digest()
            return hmac.compare_digest(want, bytes(tag))
        if self.suite == SUITE_ED25519:
            try:
                Ed25519PublicKey.from_public_bytes(self.material).verify(bytes(tag), message)
                return True
            except InvalidSignature:
                return False
        return False


@dataclass
class SigningKey:
    """Key material held by a sender: Ed25519 seed or HMAC secret."""
    key_id: bytes
    suite: int
    secret: bytes

    def sign(self, message):
        if self.suite == SUITE_HMAC:
            return hmac.new(self.secret, message, hashlib.sha256).digest()
        if self.suite == SUITE_ED25519:
            return Ed25519PrivateKey.from_private_bytes(self.secret).sign(message)
        raise ValueError("no signing algorithm for suite %s" % suite_str(self.suite))


def test_signing_key(key_id, suite=None, label=None):
    """Signing key derived from the public label of ``label`` (default: key_id).

    ``label`` lets a test sign with one key while labelling the frame with
    another key_id (wrong-key cases CMD-07, CMD-08).
    """
    secret = derive_test_key(label or key_id)
    if suite is None:
        suite = SUITE_ED25519 if "ED25519" in (label or key_id) else SUITE_HMAC
    return SigningKey(key_id.encode("ascii") if isinstance(key_id, str) else key_id, suite, secret)


class KeyStore:
    """Receiver keys, looked up by (suite_id, key_id) (A18)."""

    def __init__(self, keys=()):
        self._keys = {}
        for k in keys:
            self.add(k)

    def add(self, key):
        self._keys[(key.suite, key.key_id)] = key

    def get(self, suite, key_id):
        return self._keys.get((suite, bytes(key_id)))

    def key_ids(self):
        return sorted(k.key_id.decode("ascii", "replace") for k in self._keys.values())

    def __iter__(self):
        return iter(self._keys.values())

    @classmethod
    def from_json(cls, path):
        """Load a rover key file (endpoint-python/config/rover-keys.json)."""
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        keys = []
        for k in doc["keys"]:
            suite = parse_suite(k["suite"])
            if suite == SUITE_ED25519:
                material = bytes.fromhex(k["public_key_hex"])
            elif suite == SUITE_HMAC:
                material = bytes.fromhex(k["key_hex"])
            else:
                raise ValueError("unknown suite %s in %s" % (k["suite"], path))
            keys.append(VerifyKey(k["key_id"].encode("ascii"), suite, material,
                                  k["bound_source_id"], k["bound_authority"].encode("ascii")))
        return cls(keys)


@dataclass
class Policy:
    """Rover authorization policy (SPEC-DIGEST 5, A24)."""
    authority_id_type: int = 3
    authority_id: bytes = b"PBS-DEMO-MCC"
    policy_epoch: int = 1
    roles: dict = field(default_factory=lambda: {
        b"FLIGHT-OPS": ("PING", "SET_HEATER", "DRIVE", "CAPTURE_IMAGE", "SET_MODE"),
        b"SCIENCE-OPS": ("CAPTURE_IMAGE",),
    })

    @classmethod
    def from_json(cls, path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return cls(authority_id_type=int(doc["authority_id_type"]),
                   authority_id=doc["authority_id"].encode("ascii"),
                   policy_epoch=int(doc["policy_epoch"]),
                   roles={r.encode("ascii"): tuple(ops) for r, ops in doc["roles"].items()})


# ---------------------------------------------------------------------------
# Frame encoders and decoders
# ---------------------------------------------------------------------------

def auth_value(authority, role, epoch, id_type=3, version=AUTH_VERSION):
    """PBS-AUTH-01 Section 2 Authority Context frame value."""
    authority, role = bytes(authority), bytes(role)
    return (bytes([version, id_type, len(authority), len(role)]) + epoch.to_bytes(4, "big")
            + authority + role)


@dataclass(frozen=True)
class AuthContext:
    version: int
    id_type: int
    authority: bytes
    role: bytes
    epoch: int


def parse_auth_value(v):
    """Return AuthContext, or None when the value is malformed (R12a)."""
    v = bytes(v)
    if len(v) < AUTH_FIXED_LEN or v[0] != AUTH_VERSION or len(v) != AUTH_FIXED_LEN + v[2] + v[3]:
        return None
    return AuthContext(v[0], v[1], v[8:8 + v[2]], v[8 + v[2]:], int.from_bytes(v[4:8], "big"))


def sec_meta(suite, key_id, security_sequence, auth_len, flags=SEC_FLAG_AUTH, nonce=b"",
             version=SEC_VERSION):
    """Security frame value without auth_data (PBS-SEC-B-01 Section 4)."""
    key_id, nonce = bytes(key_id), bytes(nonce)
    return (bytes([version]) + suite.to_bytes(2, "big") + bytes([flags, len(key_id), len(nonce)])
            + auth_len.to_bytes(2, "big") + security_sequence.to_bytes(8, "big") + key_id + nonce)


@dataclass(frozen=True)
class SecFrame:
    version: int
    suite: int
    flags: int
    key_id: bytes
    nonce: bytes
    auth_len: int
    security_sequence: int
    auth_data: bytes


def parse_sec_value(v):
    """Return SecFrame, or None when the structure is malformed (R9)."""
    v = bytes(v)
    if len(v) < SEC_META_LEN or v[0] != SEC_VERSION:
        return None
    kl, nl = v[4], v[5]
    al = int.from_bytes(v[6:8], "big")
    if len(v) != SEC_META_LEN + kl + nl + al:
        return None
    return SecFrame(v[0], int.from_bytes(v[1:3], "big"), v[3], v[16:16 + kl],
                    v[16 + kl:16 + kl + nl], al, int.from_bytes(v[8:16], "big"),
                    v[16 + kl + nl:])


# ---------------------------------------------------------------------------
# Sender: protected command (SPEC-DIGEST 6.5)
# ---------------------------------------------------------------------------

def build_protected(builder, priority, ttl_s, frames_before, signing_key, security_sequence,
                    suite=None, auth_len=None, ack=False, timestamp_us=None, sequence=None):
    """Envelope whose MUX container ends with a Security frame.

    ``frames_before`` are the (type, value) frames placed before the
    Security frame (Authority Context, Command, ...). The header is built
    by PBS_LINK (``builder``) for the final Size; auth_data is computed over
    ``envelope[0 : 44 + Size - auth_len]`` and appended (A20).

    Returns ``(envelope, auth_input, auth_data)``.
    """
    suite = signing_key.suite if suite is None else suite
    al = SUITE_AUTH_LEN.get(suite, 32) if auth_len is None else auth_len
    meta = sec_meta(suite, signing_key.key_id, security_sequence, al)
    frames_before = [(t, bytes(v)) for t, v in frames_before]
    body = (build_container(frames_before, count=len(frames_before) + 1)
            + bytes([FT_SEC]) + (len(meta) + al).to_bytes(2, "big") + meta)
    placeholder = body + b"\0" * al
    header = builder.build(priority, placeholder, ttl_s, ack=ack, timestamp_us=timestamp_us,
                           sequence=sequence)[:E.HEADER_SIZE]
    auth_input = header + body
    tag = signing_key.sign(auth_input)[:al]
    return auth_input + tag, auth_input, tag


def build_command(builder, command, signing_key, security_sequence, role=b"FLIGHT-OPS",
                  authority=b"PBS-DEMO-MCC", epoch=1, id_type=3, priority=1, ttl_s=30,
                  timestamp_us=None, sequence=None, extra_frames=()):
    """Earth-style protected command: Authority Context, [extra], Command, Security."""
    if isinstance(command, str):
        command = command.encode("ascii")
    frames = [(FT_AUTH, auth_value(authority, role, epoch, id_type))]
    frames += [(t, bytes(v)) for t, v in extra_frames]
    frames.append((FT_COMMAND, command))
    env, _, _ = build_protected(builder, priority, ttl_s, frames, signing_key, security_sequence,
                                timestamp_us=timestamp_us, sequence=sequence)
    return env


# ---------------------------------------------------------------------------
# Replay window (SPEC-DIGEST 6.4, A22)
# ---------------------------------------------------------------------------

class ReplayWindow:
    """Per (Source ID raw 16 bytes, key_id): highest accepted value H and
    the accepted values in (H - 64, H]."""

    def __init__(self, size=REPLAY_WINDOW):
        self.size = size
        self._state = {}

    def check(self, key, s):
        hi, seen = self._state.get(key, (0, frozenset()))
        if s == 0:
            return False
        if s > hi:
            return True
        return s > hi - self.size and s not in seen

    def mark(self, key, s):
        hi, seen = self._state.get(key, (0, frozenset()))
        hi = max(hi, s)
        self._state[key] = (hi, frozenset(x for x in set(seen) | {s} if x > hi - self.size))


# ---------------------------------------------------------------------------
# Receiver (SPEC-DIGEST 6.6)
# ---------------------------------------------------------------------------

@dataclass
class Disposition:
    result: str
    reason: str
    header: object = None               # envelope.Header or None
    payload: bytes = None
    frames: list = None                 # list of mux.Frame, None if not parsed
    padding: int = None
    sec: SecFrame = None                # set once R9 passed
    auth: AuthContext = None            # set once R12a passed
    commands: list = None               # Command frame values (bytes), when accepted

    @property
    def accepted(self):
        return self.result == "ACCEPT"

    @property
    def frame_types(self):
        return None if self.frames is None else [f.type for f in self.frames]


DATA_FRAME_TYPES = (FT_TELEMETRY, FT_STATUS, FT_EVENT)


class Receiver:
    """Receive procedure R1 to R14 with one replay state.

    ``keys`` is a KeyStore of verification keys, ``policy`` a Policy.
    """

    def __init__(self, keys, policy=None, replay_window=REPLAY_WINDOW):
        self.keys = keys
        self.policy = policy or Policy()
        self.replay = ReplayWindow(replay_window)

    def receive(self, data, now):
        data = bytes(data)
        # R1-R6: PBS-ENV-01 Section 14 (PBS_LINK parse + demo checks)
        reason, hdr, payload = E.check_envelope(data, now)
        if reason != "OK":
            return Disposition("REJECT", reason, hdr)
        d = Disposition("REJECT", "", hdr, payload)
        # R6a: header-only message
        if hdr.size == 0:
            d.frames, d.padding = [], 0
            return self._done(d, "ACCEPT", "OK")
        # R7: MUX container
        try:
            frames, padding = parse_container(payload)
        except MuxError as ex:
            return self._done(d, "REJECT", ex.reason)
        d.frames, d.padding = frames, padding
        types = [f.type for f in frames]
        has_cmd = FT_COMMAND in types
        secs = [f for f in frames if f.type == FT_SEC]
        # R8: unprotected data / R8a: unprotected command
        if not secs and not has_cmd:
            return self._done(d, "ACCEPT", "OK")
        if has_cmd and not secs:
            return self._done(d, "REJECT", "SEC_MISSING")
        # R9: one Security frame, last, no padding, consistent lengths
        if len(secs) != 1 or frames[-1].type != FT_SEC or padding != 0:
            return self._done(d, "REJECT", "SEC_MALFORMED")
        sec = parse_sec_value(frames[-1].value)
        if sec is None:
            return self._done(d, "REJECT", "SEC_MALFORMED")
        d.sec = sec
        # R9a: suite
        if sec.suite not in SUITE_AUTH_LEN:
            return self._done(d, "REJECT", "SEC_SUITE_UNKNOWN")
        # R9b: suite parameters
        if (sec.flags != SEC_FLAG_AUTH or len(sec.nonce) != 0
                or sec.auth_len != SUITE_AUTH_LEN[sec.suite] or len(sec.key_id) == 0):
            return self._done(d, "REJECT", "SEC_MALFORMED")
        # R9c: key provisioned
        key = self.keys.get(sec.suite, sec.key_id)
        if key is None:
            return self._done(d, "REJECT", "SEC_KEY_UNKNOWN")
        # R10: authentication over envelope[0 : 44 + Size - auth_len]
        cut = len(data) - sec.auth_len
        if not key.verify(data[:cut], data[cut:]):
            return self._done(d, "REJECT", "SEC_AUTH_FAIL")
        # R11: replay window, then record
        rkey = (hdr.source_id_raw, bytes(sec.key_id))
        if not self.replay.check(rkey, sec.security_sequence):
            return self._done(d, "REJECT", "SEC_REPLAY")
        self.replay.mark(rkey, sec.security_sequence)
        # R11a: protected data without a command
        if not has_cmd:
            return self._done(d, "ACCEPT", "OK")
        # R12: exactly one Authority Context frame
        auths = [f for f in frames if f.type == FT_AUTH]
        if not auths:
            return self._done(d, "REJECT", "AUTH_CTX_MISSING")
        if len(auths) > 1:
            return self._done(d, "REJECT", "AUTH_CTX_MALFORMED")
        ctx = parse_auth_value(auths[0].value)
        if ctx is None:
            return self._done(d, "REJECT", "AUTH_CTX_MALFORMED")
        d.auth = ctx
        # R13: authorization (source and authority bound to the key, policy)
        pol = self.policy
        if (hdr.source_id_raw != E.source_id_bytes(key.bound_source_id)
                or ctx.authority != key.bound_authority
                or ctx.authority != pol.authority_id
                or ctx.id_type != pol.authority_id_type
                or ctx.epoch != pol.policy_epoch
                or ctx.role not in pol.roles):
            return self._done(d, "REJECT", "AUTHZ_DENIED")
        cmds = [f.value for f in frames if f.type == FT_COMMAND]
        for c in cmds:
            if operation(c) not in pol.roles[ctx.role]:
                return self._done(d, "REJECT", "AUTHZ_DENIED")
        # R14: accept; commands execute in frame order
        d.commands = cmds
        return self._done(d, "ACCEPT", "OK")

    @staticmethod
    def _done(d, result, reason):
        d.result, d.reason = result, reason
        return d


def operation(command_value):
    """Command operation: the value up to the first space (SPEC-DIGEST 5)."""
    return bytes(command_value).split(b" ", 1)[0].decode("ascii", "replace")


def rover_receiver(keys_path, policy_path):
    return Receiver(KeyStore.from_json(keys_path), Policy.from_json(policy_path))
