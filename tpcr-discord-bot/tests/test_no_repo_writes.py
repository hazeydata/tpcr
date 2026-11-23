import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ask_agent as aa  # noqa: E402

BOT_DIR = os.path.join(os.path.dirname(__file__), "..")
TRACKED = ("ask_usage.json", "ask_questions.jsonl", "ask_feedback.jsonl")


def _digests():
    out = {}
    for name in TRACKED:
        path = os.path.join(BOT_DIR, name)
        out[name] = hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else None
    return out


def test_ask_writers_go_to_tmp_not_repo(tmp_path):
    before = _digests()
    aa.track_usage("u")
    aa.log_question("u", "user", "q", "a", 1)
    assert str(aa._usage_file()).startswith(str(tmp_path))
    assert str(aa._question_log()).startswith(str(tmp_path))
    assert (tmp_path / "ask_usage.json").exists()
    assert (tmp_path / "ask_questions.jsonl").exists()
    assert _digests() == before
