import os
from pathlib import Path

import pytest

from web.private_cookie_store import (
    CookieCredentialUnavailable,
    PrivateCookieStore,
)


COOKIE_BYTES = (
    b"# Netscape HTTP Cookie File\n"
    b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\ttest-value\n"
)


def make_store(tmp_path: Path) -> PrivateCookieStore:
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    return PrivateCookieStore(
        tmp_path / "private",
        legacy_jobs_root=jobs,
        enforce_os_acl=False,
    )


def test_cookie_is_memory_only_until_worker_materializes_it(tmp_path):
    store = make_store(tmp_path)
    try:
        store.queue("job123", 4, COOKIE_BYTES)
        assert store.has_pending("job123", 4)
        assert not list((tmp_path / "private").glob("*.cookie"))

        path = store.materialize("job123", 4)
        assert path.parent == (tmp_path / "private")
        assert path.read_bytes() == COOKIE_BYTES
        assert not store.has_pending("job123", 4)

        store.cleanup_materialized(path)
        assert not path.exists()
    finally:
        store.close()


def test_cookie_store_discards_queued_and_missing_generations(tmp_path):
    store = make_store(tmp_path)
    try:
        store.queue("job123", 1, COOKIE_BYTES)
        store.queue("job123", 2, COOKIE_BYTES)
        store.discard("job123", 1)
        assert not store.has_pending("job123", 1)
        assert store.has_pending("job123", 2)
        with pytest.raises(CookieCredentialUnavailable):
            store.materialize("job123", 1)
        store.discard("job123")
        assert not store.has_pending("job123", 2)
    finally:
        store.close()


def test_startup_cleans_legacy_job_cookie_and_crash_leftover(tmp_path):
    jobs = tmp_path / "jobs"
    legacy = jobs / "oldjob" / "youtube.cookies.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(COOKIE_BYTES)
    private = tmp_path / "private"
    private.mkdir()
    stale = private / "subtitle-cookie-stale.cookie"
    stale.write_bytes(COOKIE_BYTES)
    store = PrivateCookieStore(
        private,
        legacy_jobs_root=jobs,
        enforce_os_acl=False,
    )
    try:
        store.initialize()
        assert not legacy.exists()
        assert not stale.exists()
    finally:
        store.close()


def test_cookie_store_lock_prevents_second_instance_cleanup(tmp_path):
    first = make_store(tmp_path)
    second = make_store(tmp_path)
    try:
        first.initialize()
        with pytest.raises(RuntimeError, match="另一个工作台实例"):
            second.initialize()
    finally:
        second.close()
        first.close()


def test_close_zeroes_pending_deletes_materialized_and_releases_lock(tmp_path):
    store = make_store(tmp_path)
    store.queue("pending", 1, COOKIE_BYTES)
    pending = store._pending[("pending", 1)]
    store.queue("materialized", 2, COOKIE_BYTES)
    path = store.materialize("materialized", 2)

    store.close()

    assert pending == bytearray()
    assert store._pending == {}
    assert store._materialized == set()
    assert not path.exists()
    assert store._lock_handle is None
    assert store._initialized is False

    replacement = make_store(tmp_path)
    try:
        replacement.initialize()
    finally:
        replacement.close()
    store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration")
def test_windows_private_store_applies_current_user_acl_before_write(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    store = PrivateCookieStore(
        tmp_path / "windows-private",
        legacy_jobs_root=jobs,
        enforce_os_acl=True,
    )
    try:
        store.queue("job123", 7, COOKIE_BYTES)
        path = store.materialize("job123", 7)
        assert path.read_bytes() == COOKIE_BYTES
        store.cleanup_materialized(path)
    finally:
        store.close()
