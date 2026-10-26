#!/usr/bin/env python3
"""Approval-flow signing helper for x_approved_reply_poster.py (spec R5 / AC 3c).

Only the approval flow runs this. It holds the Ed25519 PRIVATE key; the poster holds only the
public key. The private key path is given at runtime (--key); it is never read from env, and
the helper refuses a key file that is group/world-readable, inside the queue dir, or inside a
git work tree.

    x_reply_sign_approval.py keygen --out PRIV.pem --pub PUB.pem    # Fred-gated setup step, once
    x_reply_sign_approval.py sign --key PRIV.pem ENTRY.json [...]    # adds approval.signature
"""

import argparse
import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import x_approved_reply_poster as poster  # noqa: E402


def _in_git_worktree(path):
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=Path(path).parent,
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def check_key_path(key_path, queue_dirs=()):
    p = Path(key_path).resolve()
    if not p.is_file():
        raise SystemExit("signing key not found")
    if stat.S_IMODE(os.stat(p).st_mode) & 0o077:
        raise SystemExit("signing key must be mode 0600 (no group/world access)")
    for q in queue_dirs:
        if Path(q).resolve() in p.parents:
            raise SystemExit("signing key must not live in the queue dir")
    if _in_git_worktree(p):
        raise SystemExit("signing key must not live in a git work tree")
    return p


def load_signing_key(key_path, queue_dirs=()):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    p = check_key_path(key_path, queue_dirs)
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("signing key is not Ed25519")
    return key


def sign_entry(entry, queue_dir, key):
    """Return the base64 signature for a hash-consistent entry (refuses to sign a mismatched one)."""
    if not isinstance(entry.get("approval"), dict):
        raise ValueError("approval is not an object")
    if not poster.check_hash(entry):
        raise ValueError("text hash mismatch; refusing to sign")
    return base64.b64encode(key.sign(poster.approval_message(entry, queue_dir))).decode("ascii")


def sign_file(path, key):
    p = Path(path)
    entry = json.loads(p.read_text(encoding="utf-8"))
    entry["approval"]["signature"] = sign_entry(entry, p.parent, key)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def keygen(out, pub):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    out, pub = Path(out), Path(pub)
    if out.exists() or pub.exists():
        raise SystemExit("refusing to overwrite an existing key file")
    key = Ed25519PrivateKey.generate()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    try:
        check_key_path(out)
    except SystemExit:
        out.unlink()
        pub.unlink()
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("keygen")
    g.add_argument("--out", required=True)
    g.add_argument("--pub", required=True)
    s = sub.add_parser("sign")
    s.add_argument("--key", required=True, help="Ed25519 private key (PEM), path given at runtime")
    s.add_argument("entries", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        keygen(a.out, a.pub)
        return 0
    key = load_signing_key(a.key, {str(Path(e).resolve().parent) for e in a.entries})
    for e in a.entries:
        sign_file(e, key)
        print(f"signed {Path(e).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
