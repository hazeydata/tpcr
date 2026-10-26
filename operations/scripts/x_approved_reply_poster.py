#!/usr/bin/env python3
"""X approved-reply poster (X-APPROVED-REPLY-POSTER-001).

Posts an X reply only when the exact text carries Fred's verbatim, hash-bound
approval. Dry run is the default; ``--apply`` is required to post. Full contract:
operations/scripts/x_approved_reply_poster.md.

Status (spec OQ-2): kept in the append-only ledger, never in the queue. The queue
is opened read-only. ``effective_status()`` derives an entry's status from its
latest ledger line.

Signed approvals (spec R5 / AC 3c): every approval carries an Ed25519 signature made by
the approval flow (x_reply_sign_approval.py). The poster holds only the PUBLIC key and
refuses an unsigned or badly signed approval (ledger ``refused_unsigned``, exit 1).
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = "HazeyDataFred"
MAX_PER_RUN = 5
MIN_GAP_SECONDS = 30
MAX_WEIGHTED = 280
MAX_ALT = 1000
DIGITS = re.compile(r"^[0-9]+$")
ENTRY_ID = re.compile(r"^[A-Za-z0-9._-]+$")
URL_RE = re.compile(r"https?://\S+", re.I)


class ApiError(Exception):
    """HTTP failure from X. Carries only the status code (never request auth)."""

    def __init__(self, status, where):
        super().__init__(f"{where}: HTTP {status}")
        self.status = status
        self.where = where


@dataclass
class Outcome:
    entry_id: str
    text_sha256: str
    outcome: str
    at: str = ""
    post_url: str = None
    http_status: int = None
    detail: str = None
    payload: dict = None
    manual_url: str = None
    manual_text: str = None
    step: str = None
    queue_commit: str = None
    calls: list = field(default_factory=list)

    def ledger_line(self):
        d = {k: v for k, v in asdict(self).items() if v is not None and k not in ("calls", "manual_text")}
        return d


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def weighted_length(text):
    """X weighted length: URLs count 23; code points in X's weight-1 ranges count 1, others 2."""
    text = unicodedata.normalize("NFC", text)
    n = 0
    for m in URL_RE.finditer(text):
        n += 23
    text = URL_RE.sub("", text)
    for ch in text:
        cp = ord(ch)
        if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
            n += 1
        else:
            n += 2
    return n


# ---------------- P1: validation (no HTTP) ----------------

def check_hash(entry):
    text = entry.get("text")
    if not isinstance(text, str):
        return False
    h = sha256_text(text)
    appr = entry.get("approval")
    if not isinstance(appr, dict):
        return False  # run() rejects a non-object approval as rejected_invalid first (AC 2)
    return h == entry.get("text_sha256") and h == appr.get("text_sha256")


def validate(entry, queue_dir):
    """Return None if valid, else a reason string (-> rejected_invalid)."""
    appr = entry.get("approval")
    if not isinstance(appr, dict):
        return "approval missing"
    q = appr.get("quote")
    if not isinstance(q, str) or not q.strip():
        return "approval.quote empty"
    if appr.get("approved_by") != "Fred":
        return "approved_by is not Fred"
    at = appr.get("approved_at")
    try:
        dt = datetime.fromisoformat(at)
        if dt.tzinfo is None:
            return "approved_at has no UTC offset"
    except (TypeError, ValueError):
        return "approved_at does not parse"
    rid = entry.get("reply_to_id")
    if not isinstance(rid, str) or not DIGITS.match(rid):
        return "reply_to_id not all digits"
    if weighted_length(entry["text"]) > MAX_WEIGHTED:
        return "text over 280 weighted chars"
    img, alt = entry.get("image_path"), entry.get("alt_text")
    if alt is not None and (not isinstance(alt, str) or len(alt) > MAX_ALT):
        return "alt_text over 1000 chars"
    if img:
        if not isinstance(alt, str) or not alt.strip():
            return "image without alt_text"
        p = Path(img) if Path(img).is_absolute() else Path(queue_dir) / img
        if not p.is_file():
            return "image_path not found"
    return None


# ---------------- AC 3c: signed approvals (Ed25519, poster holds the public key only) ----------------

SIG_VERSION = "x-approved-reply-v1"


def file_sha256(path):
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def image_file(entry, queue_dir):
    img = entry.get("image_path")
    if not isinstance(img, str) or not img:
        return None
    return Path(img) if Path(img).is_absolute() else Path(queue_dir) / img


def approval_message(entry, queue_dir):
    """Canonical bytes the approval flow signs. Binds the approved text hash to the target post,
    Fred's quote/name/time, the alt text and the image bytes, so none of them can be swapped."""
    appr = entry.get("approval") if isinstance(entry.get("approval"), dict) else {}
    alt = entry.get("alt_text")
    img = image_file(entry, queue_dir)
    msg = {"v": SIG_VERSION, "entry_id": entry.get("entry_id"), "reply_to_id": entry.get("reply_to_id"),
           "text_sha256": entry.get("text_sha256"), "quote": appr.get("quote"),
           "approved_by": appr.get("approved_by"), "approved_at": appr.get("approved_at"),
           "alt_text_sha256": sha256_text(alt) if isinstance(alt, str) else None,
           "image_sha256": file_sha256(img) if img is not None else None}
    return json.dumps(msg, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_verify_key(path, queue_dir=None):
    """Load the Ed25519 PUBLIC key. Refuses a private key, a non-Ed25519 key, or a key file inside
    the queue dir (whoever can write the queue must not be able to swap the verifier)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not path:
        raise SystemExit("refusing to run: no approval verify key (--verify-key or X_REPLY_VERIFY_KEY) (AC 3c)")
    p = Path(path).resolve()
    if queue_dir is not None and Path(queue_dir).resolve() in p.parents:
        raise SystemExit("refusing to run: verify key must not live in the queue dir (AC 3c)")
    try:
        data = p.read_bytes()
    except OSError as e:
        raise SystemExit(f"refusing to run: cannot read verify key ({e.__class__.__name__}) (AC 3c)")
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError:
        try:
            serialization.load_pem_private_key(data, password=None)
            is_private = True
        except (ValueError, TypeError):
            is_private = False
        if is_private:
            raise SystemExit("refusing to run: verify key is a PRIVATE key; the poster must hold only the public key (AC 3c)")
        raise SystemExit("refusing to run: verify key is not a PEM public key (AC 3c)")
    if not isinstance(key, Ed25519PublicKey):
        raise SystemExit("refusing to run: verify key is not Ed25519 (AC 3c)")
    return key


def signature_ok(entry, queue_dir, verify_key):
    from cryptography.exceptions import InvalidSignature
    sig = (entry.get("approval") or {}).get("signature")
    if not isinstance(sig, str) or not sig:
        return False
    try:
        raw = base64.b64decode(sig, validate=True)
        verify_key.verify(raw, approval_message(entry, queue_dir))
        return True
    except (ValueError, InvalidSignature):
        return False


# ---------------- ledger ----------------

def read_ledger(path):
    """Parse ledger lines; a torn trailing line (crash mid-write) is ignored."""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_ledger(fh, outcome):
    fh.write(json.dumps(outcome.ledger_line(), ensure_ascii=False, sort_keys=True) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


STATUS_FROM_OUTCOME = {"posted": "posted", "skipped_duplicate": "posted",
                       "manual_not_mentioned": "manual", "api_error": "failed"}


def effective_status(entry, ledger):
    """OQ-2: status = latest status-bearing ledger line for (entry_id, text_sha256),
    else the queue's own status. dry_run / rejected_* do not change status."""
    st = entry.get("status")
    for rec in ledger:
        if rec.get("entry_id") == entry.get("entry_id") and rec.get("text_sha256") == entry.get("text_sha256"):
            st = STATUS_FROM_OUTCOME.get(rec.get("outcome"), st)
            if (rec.get("outcome") == "api_error" and rec.get("http_status") == 403
                    and rec.get("step") == "POST tweet"):
                st = "manual"  # AC 6a: X refused the POST; Fred posts by hand. 403 elsewhere -> failed
    return st


# ---------------- P2: mention pre-check ----------------

def mention_ok(tweet_resp):
    data = tweet_resp.get("data") or {}
    users = {u.get("id"): (u.get("username") or "").lower() for u in (tweet_resp.get("includes") or {}).get("users", [])}
    for m in (data.get("entities") or {}).get("mentions", []) or []:
        if (m.get("username") or "").lower() == ACCOUNT.lower():
            return True
    quoted = {r.get("id") for r in data.get("referenced_tweets") or [] if r.get("type") == "quoted"}
    for t in (tweet_resp.get("includes") or {}).get("tweets", []) or []:
        if t.get("id") in quoted and users.get(t.get("author_id")) == ACCOUNT.lower():
            return True
    return False


# ---------------- run ----------------

def check_queue_dir(queue_dir):
    """AC 3a: refuse to run if the queue folder is group- or world-writable."""
    mode = os.stat(queue_dir).st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit(f"refusing to run: queue dir {queue_dir} is group/world-writable (mode {stat.S_IMODE(mode):o})")


def queue_commit_sha(path):
    """AC 3b: git commit SHA of the last commit touching the queue file, or "uncommitted".
    Local git only (no network). Uncommitted or dirty files are reported, not hidden."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%H", "--", p.name], cwd=p.parent,
                           capture_output=True, text=True, timeout=10)
        sha = r.stdout.strip() if r.returncode == 0 else ""
        if not sha:
            return "uncommitted"
        d = subprocess.run(["git", "status", "--porcelain", "--", p.name], cwd=p.parent,
                           capture_output=True, text=True, timeout=10)
        return sha + ("+dirty" if d.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "uncommitted"


def load_queue(queue_dir):
    entries = []
    for p in sorted(Path(queue_dir).glob("*.json")):
        with open(p, "rb") as fh:  # read-only: the poster never writes the queue
            raw = fh.read()
        try:
            e = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            e = {"entry_id": p.stem, "_unparseable": True}
        if not isinstance(e, dict):
            e = {"entry_id": p.stem, "_unparseable": True}
        e["_path"] = str(p)
        entries.append(e)
    return entries


def run(queue_dir, ledger_path, apply, client, offline=False, max_posts=MAX_PER_RUN,
        sleep=time.sleep, out=sys.stdout, verify_key=None):
    if apply and offline:
        raise ValueError("--apply and --offline are exclusive")
    max_posts = min(int(max_posts), MAX_PER_RUN)
    check_queue_dir(queue_dir)
    if verify_key is None:  # AC 3c: no verifier -> refuse before the ledger or X
        raise SystemExit("refusing to run: no approval verify key (AC 3c)")
    results = []
    Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as lfh:
        try:
            fcntl.flock(lfh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another poster run holds the ledger lock")
        ledger = read_ledger(ledger_path)
        if lfh.tell() and not Path(ledger_path).read_bytes().endswith(b"\n"):
            lfh.write("\n")  # close a torn line left by a crash so new lines parse
        posted = 0
        last_post = None

        def emit(o):
            o.at = now_iso()
            append_ledger(lfh, o)
            ledger.append(o.ledger_line())
            results.append(o)
            return o

        for entry in load_queue(queue_dir):
            eid = str(entry.get("entry_id", ""))
            sha = entry.get("text_sha256") if isinstance(entry.get("text_sha256"), str) else ""
            if entry.get("_unparseable") or not ENTRY_ID.match(eid):
                emit(Outcome(eid, sha, "rejected_invalid", detail="unparseable entry or bad entry_id"))
                continue
            # AC 9: duplicate check first, 0 calls
            if any(r.get("entry_id") == eid and r.get("text_sha256") == sha and r.get("outcome") in ("posted", "skipped_duplicate")
                   for r in ledger):
                emit(Outcome(eid, sha, "skipped_duplicate"))
                continue
            st = effective_status(entry, ledger)
            if st != "approved":
                continue  # manual / failed / posted entries wait for a human
            # AC 2: a non-object approval (e.g. "yes") is invalid, not a crash
            if not isinstance(entry.get("approval"), dict):
                emit(Outcome(eid, sha, "rejected_invalid", detail="approval is not an object"))
                continue
            # AC 1
            if not check_hash(entry):
                emit(Outcome(eid, sha, "rejected_hash_mismatch"))
                continue
            # AC 3c: Ed25519 signature from the approval flow, 0 calls if missing or bad
            if not signature_ok(entry, queue_dir, verify_key):
                emit(Outcome(eid, sha, "refused_unsigned", detail="approval unsigned or signature invalid"))
                continue
            # AC 2
            reason = validate(entry, queue_dir)
            if reason:
                emit(Outcome(eid, sha, "rejected_invalid", detail=reason))
                continue
            rid, text = entry["reply_to_id"], entry["text"]
            payload = {"text": text, "reply": {"in_reply_to_tweet_id": rid}}
            img = entry.get("image_path")

            # AC 4/6: one GET of the target
            if not offline:
                try:
                    tw = client.get_tweet(rid)
                except ApiError as e:
                    emit(Outcome(eid, sha, "api_error", http_status=e.status, step=e.where,
                             detail="mention pre-check GET failed; run stopped"))
                    break
                if not mention_ok(tw):
                    murl = f"https://x.com/i/status/{rid}"
                    o = emit(Outcome(eid, sha, "manual_not_mentioned", manual_url=murl, manual_text=text))
                    print(f"MANUAL {eid}: {murl}\n{text}", file=out)
                    continue

            if not apply:  # AC 7
                p = dict(payload)
                if img:
                    p["media"] = {"image_path": img, "alt_text": entry.get("alt_text")}
                emit(Outcome(eid, sha, "dry_run", payload=p,
                             detail="offline: mention pre-check skipped" if offline else "mention pre-check passed"))
                continue

            if posted >= max_posts:  # AC 11
                break

            # AC 9 crash recovery: identical reply already on X?
            try:
                for r in client.recent_replies(rid) or []:
                    if r.get("text") == text:
                        emit(Outcome(eid, sha, "posted", post_url=f"https://x.com/{ACCOUNT}/status/{r['id']}",
                                     detail="recovered: found existing identical reply",
                                     queue_commit=queue_commit_sha(entry["_path"])))
                        break
                else:
                    r = None
            except ApiError as e:
                emit(Outcome(eid, sha, "api_error", http_status=e.status, step=e.where, detail="recent-replies GET failed"))
                if e.status == 429:
                    break
                continue
            if results and results[-1].entry_id == eid and results[-1].outcome == "posted":
                continue

            if last_post is not None:
                gap = time.monotonic() - last_post
                if gap < MIN_GAP_SECONDS:
                    sleep(MIN_GAP_SECONDS - gap)
            qc = queue_commit_sha(entry["_path"])
            try:
                if img:
                    p = Path(img) if Path(img).is_absolute() else Path(queue_dir) / img
                    mid = client.upload_media(str(p))
                    client.media_metadata(mid, entry["alt_text"])
                    payload["media"] = {"media_ids": [str(mid)]}
                tid = client.post_reply(payload)
            except ApiError as e:
                if e.status == 403 and e.where == "POST tweet":  # AC 6a: no probing, hand to Fred
                    murl = f"https://x.com/i/status/{rid}"
                    emit(Outcome(eid, sha, "api_error", http_status=403, detail="POST 403: marked manual",
                                 step=e.where, queue_commit=qc, manual_url=murl, manual_text=text))
                    print(f"MANUAL {eid}: {murl}\n{text}", file=out)
                    continue
                emit(Outcome(eid, sha, "api_error", http_status=e.status, detail=e.where, step=e.where, queue_commit=qc))
                if e.status == 429:
                    break
                continue
            last_post = time.monotonic()
            posted += 1
            emit(Outcome(eid, sha, "posted", post_url=f"https://x.com/{ACCOUNT}/status/{tid}", queue_commit=qc))
    return results


# ---------------- real client (OAuth 1.0a, TWITTER_* env as wti_observed_tweet.py) ----------------

class XClient:
    API = "https://api.twitter.com/2"
    UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"
    META = "https://upload.twitter.com/1.1/media/metadata/create.json"

    def __init__(self):
        import requests
        from requests_oauthlib import OAuth1
        self._r = requests
        self._auth = OAuth1(os.environ["TWITTER_CONSUMER_KEY"], os.environ["TWITTER_CONSUMER_SECRET"],
                            os.environ["TWITTER_ACCESS_TOKEN"], os.environ["TWITTER_ACCESS_TOKEN_SECRET"])
        self._me = None

    def _ok(self, resp, where):
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, where)
        return resp.json() if resp.content else {}

    def get_tweet(self, tid):
        return self._ok(self._r.get(f"{self.API}/tweets/{tid}", auth=self._auth, timeout=30, params={
            "tweet.fields": "author_id,entities,referenced_tweets",
            "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
            "user.fields": "username"}), "GET tweet")

    def recent_replies(self, tid):
        if self._me is None:
            self._me = self._ok(self._r.get(f"{self.API}/users/me", auth=self._auth, timeout=30), "GET me")["data"]["id"]
        d = self._ok(self._r.get(f"{self.API}/users/{self._me}/tweets", auth=self._auth, timeout=30, params={
            "max_results": 100, "tweet.fields": "referenced_tweets"}), "GET recent replies")
        return [t for t in d.get("data", []) if any(r.get("type") == "replied_to" and r.get("id") == tid
                                                     for r in t.get("referenced_tweets") or [])]

    def upload_media(self, path):
        with open(path, "rb") as fh:
            d = self._ok(self._r.post(self.UPLOAD, auth=self._auth, files={"media": fh}, timeout=60), "media upload")
        return d["media_id_string"]

    def media_metadata(self, media_id, alt):
        self._ok(self._r.post(self.META, auth=self._auth, json={"media_id": media_id, "alt_text": {"text": alt}},
                              timeout=30), "media metadata")

    def post_reply(self, payload):
        return self._ok(self._r.post(f"{self.API}/tweets", auth=self._auth, json=payload, timeout=30), "POST tweet")["data"]["id"]


def main(argv=None):
    here = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queue-dir", default=str(here / "x_reply_queue"))
    ap.add_argument("--ledger", default=str(here / "x_reply_ledger.jsonl"))
    ap.add_argument("--apply", action="store_true", help="actually post (needs Fred's go via chief)")
    ap.add_argument("--offline", action="store_true", help="dry run with no HTTP at all (skips the mention GET)")
    ap.add_argument("--verify-key", default=os.environ.get("X_REPLY_VERIFY_KEY"),
                    help="Ed25519 PUBLIC key (PEM) of the approval flow; required (AC 3c)")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN, help="cap on posts this run (hard max 5)")
    a = ap.parse_args(argv)
    if a.apply and a.offline:
        ap.error("--apply and --offline are exclusive")
    vk = load_verify_key(a.verify_key, a.queue_dir)
    client = None if a.offline else XClient()
    res = run(a.queue_dir, a.ledger, a.apply, client, offline=a.offline, max_posts=a.max, verify_key=vk)
    for o in res:
        print(json.dumps(o.ledger_line(), ensure_ascii=False, sort_keys=True))
    return 1 if any(o.outcome in ("api_error", "refused_unsigned") for o in res) else 0


if __name__ == "__main__":
    sys.exit(main())
