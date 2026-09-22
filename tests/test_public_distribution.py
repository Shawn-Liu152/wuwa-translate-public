"""Privacy boundaries for the independent public source repository."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_index_excludes_private_corpora_and_runtime_state():
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    names = [name for name in listed.decode().split("\0") if name]
    assert names
    forbidden_roots = {"reports", "output", "archive", "benchmarks", "downloads", "results", ".runtime"}
    for name in names:
        path = Path(name)
        assert path.parts[0] not in forbidden_roots
        assert path.name not in {"job.json", "youtube_cookies.txt", ".visual_token.txt"}
        assert not path.name.startswith(".env")
        if path.parts[0] == "download":
            assert name == "download/.gitkeep"


def test_public_translation_memory_contains_no_private_entries():
    # Inspect the index, not mutable runtime state produced during tests.
    raw = subprocess.check_output(
        ["git", "show", ":data/translation_memory.json"], cwd=ROOT
    )
    payload = json.loads(raw)
    assert payload == {"schema_version": 2, "kind": "translation-memory", "entries": []}


def test_public_glossary_has_no_secret_or_identity_fields():
    from tools.audit_release_sqlite import audit_database

    assert audit_database(ROOT / "data" / "glossary.db")["hit_count"] == 0
