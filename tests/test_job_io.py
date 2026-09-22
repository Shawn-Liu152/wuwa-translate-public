"""I/O bounds and invalidation use disposable files, never real jobs or services."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from web import jobs as jobs_module
from web.job_change_cache import JobChangeCache


class Store:
    def close(self):
        pass


@pytest.fixture
def io_manager(tmp_path, monkeypatch):
    root = tmp_path / 'jobs'
    root.mkdir()
    monkeypatch.setattr(jobs_module, 'JOBS_ROOT', root)
    manager = jobs_module.JobManager(Store())
    clock = [1.0]
    manager._job_change_cache = JobChangeCache(clock=lambda: clock[0])
    for i in range(4):
        folder = root / f'job-{i}'
        folder.mkdir()
        manager._save({
            'id': folder.name, 'created_at': jobs_module.now(),
            'updated_at': jobs_module.now(), 'status': 'completed',
            'delivery_status': 'deliverable', 'options': {}, 'artifacts': {},
        })
    yield manager, root, clock
    manager.close()


def watch_reads(manager, root, monkeypatch):
    loads, scans = [], []
    load = manager._load
    glob = Path.glob

    def observed_load(job_id):
        loads.append(job_id)
        return load(job_id)

    def observed_glob(path, pattern):
        if path == root:
            scans.append(pattern)
        return glob(path, pattern)

    monkeypatch.setattr(manager, '_load', observed_load)
    monkeypatch.setattr(Path, 'glob', observed_glob)
    return loads, scans


def test_eight_sse_clients_share_scan_and_unchanged_records_are_not_reread(io_manager, monkeypatch):
    manager, root, clock = io_manager
    loads, scans = watch_reads(manager, root, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _: manager.job_change_snapshot(), range(8)))
    assert len(loads) == 4
    assert len(scans) == 1
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    clock[0] += 0.51
    assert manager.job_change_snapshot() == snapshots[0]
    assert len(scans) == 2
    assert len(loads) == 4


def test_saved_progress_invalidates_only_changed_record_immediately(io_manager, monkeypatch):
    manager, root, _ = io_manager
    before = manager.job_change_snapshot()
    job = manager._load('job-1')
    job['progress'] = {'completed': 1, 'total': 2}
    manager._save(job)
    loads, _ = watch_reads(manager, root, monkeypatch)
    after = manager.job_change_snapshot()
    assert loads == ['job-1']
    assert after['job-1'] != before['job-1']
    assert after['job-0'] == before['job-0']
    after['job-0']['revision'] = 'caller-mutation'
    assert manager.job_change_snapshot()['job-0'] == before['job-0']


def test_external_change_addition_and_removal_reconcile_after_refresh(io_manager):
    manager, root, clock = io_manager
    before = manager.job_change_snapshot()
    path = root / 'job-1/job.json'
    job = json.loads(path.read_text(encoding='utf-8'))
    job['progress'] = {'completed': 1234}
    path.write_text(json.dumps(job), encoding='utf-8')
    extra = root / 'extra'
    extra.mkdir()
    (extra / 'job.json').write_text(json.dumps({**job, 'id':'extra'}), encoding='utf-8')
    (root / 'job-2/job.json').unlink()
    clock[0] += 0.51
    after = manager.job_change_snapshot()
    assert after['job-1'] != before['job-1']
    assert 'extra' in after and 'job-2' not in after
    assert 'job-2' not in manager._job_change_cache._entries


def test_corrupt_record_is_evicted_and_recovers_without_restarting(io_manager):
    manager, root, clock = io_manager
    manager.job_change_snapshot()
    path = root / 'job-0/job.json'
    original = path.read_bytes()
    path.write_text('{', encoding='utf-8')
    clock[0] += 0.51
    assert 'job-0' not in manager.job_change_snapshot()
    path.write_bytes(original)
    clock[0] += 0.51
    assert 'job-0' in manager.job_change_snapshot()


def test_restart_rebuilds_same_minimal_snapshot(io_manager):
    manager, root, _ = io_manager
    before = manager.job_change_snapshot()
    restarted = JobChangeCache()
    assert restarted.snapshot(root, manager._load) == before
    assert all(set(row) == {'revision','generation','updated_at'} for row in before.values())


def install_completed_video(manager, root, job_id):
    folder = root / job_id
    video = folder / 'video.mp4'
    subtitle = folder / 'final.zh.srt'
    output = folder / 'final.zh.burned.mp4'
    video.write_bytes(b'synthetic media')
    subtitle.write_bytes(b'synthetic subtitle')
    output.write_bytes(b'synthetic output')
    job = manager._load(job_id)
    job['artifacts'] = {'video':str(video), subtitle.name:str(subtitle), output.name:str(output)}
    job['exports'] = {'burned_video':{
        'status':'completed', 'input_fingerprint':manager._burn_input_fingerprint(video, subtitle),
    }}
    manager._save(job)
    return subtitle


def test_list_never_hashes_media_or_recursively_sizes_workspaces(io_manager, monkeypatch):
    manager, root, _ = io_manager
    for job_id in ['job-0','job-1']:
        install_completed_video(manager, root, job_id)
    hashes, traversals = [], []
    fingerprint = manager._burn_input_fingerprint
    rglob = Path.rglob
    monkeypatch.setattr(manager, '_burn_input_fingerprint', lambda *args: hashes.append(True) or fingerprint(*args))
    monkeypatch.setattr(Path, 'rglob', lambda path, pattern: traversals.append(path) or rglob(path,pattern))
    listed = manager.list()
    assert len(listed) == 4
    assert hashes == [] and traversals == []
    assert all(row['summary_only'] and row['disk_bytes'] is None for row in listed)
    assert all(row['burned_video_current'] is False for row in listed)
    detail = manager.get('job-0')
    assert detail['summary_only'] is False
    assert detail['burned_video_current'] is True and detail['disk_bytes'] > 0
    assert len(hashes) == 1 and len(traversals) == 1


def test_input_changes_still_force_strict_export_validation(io_manager, monkeypatch):
    manager, root, _ = io_manager
    subtitle = install_completed_video(manager, root, 'job-0')
    assert manager.get('job-0')['burned_video_current'] is True
    subtitle.write_bytes(b'changed synthetic subtitle')
    manager.list()
    assert manager.get('job-0')['burned_video_current'] is False
    submitted = []
    monkeypatch.setattr(manager, '_submit_burn', lambda *args: submitted.append(args))
    export = manager.request_burned_video('job-0')
    assert export['exports']['burned_video']['status'] == 'queued'
    assert len(submitted) == 1


def test_summary_only_is_not_persisted(io_manager):
    manager, root, _ = io_manager
    summary = manager.list()[0]
    manager._save(summary)
    saved = json.loads((root / summary['id'] / 'job.json').read_text(encoding='utf-8'))
    assert 'summary_only' not in saved


def test_single_task_summary_does_not_verify_unselected_video(io_manager, monkeypatch):
    manager, root, _ = io_manager
    install_completed_video(manager, root, 'job-0')
    def unexpected(*args):
        pytest.fail('background card must not hash media')
    monkeypatch.setattr(manager, '_burn_input_fingerprint', unexpected)
    summary = manager.get('job-0', detailed=False)
    assert summary['summary_only'] is True
    assert summary['burned_video_current'] is False


def test_summary_api_remains_authenticated_and_default_detail_verifies_media(io_manager, monkeypatch):
    import secrets
    from fastapi.testclient import TestClient
    from web import app as app_module
    manager, root, _ = io_manager
    install_completed_video(manager, root, 'job-0')
    monkeypatch.setattr(app_module, 'manager', manager)
    tokens = [secrets.token_urlsafe(32) for _ in range(4)]
    app_module.configure_local_access(session_token=tokens[0], bootstrap_token=tokens[1],
                                     csrf_token=tokens[2], launch_challenge=tokens[3])
    # No lifespan startup: the test only exercises the read-only HTTP boundary.
    client = TestClient(app_module.app)
    try:
        assert client.get('/api/jobs/job-0?summary=true').status_code == 401
        assert client.post('/api/session/bootstrap', headers={
            'X-Subtitle-Bootstrap':tokens[1], 'Origin':'http://testserver',
        }).status_code == 200
        hashes = []
        fingerprint = manager._burn_input_fingerprint
        monkeypatch.setattr(manager, '_burn_input_fingerprint', lambda *args: hashes.append(True) or fingerprint(*args))
        summary = client.get('/api/jobs/job-0?summary=true')
        assert summary.status_code == 200
        assert summary.json()['summary_only'] is True
        assert hashes == []
        detail = client.get('/api/jobs/job-0')
        assert detail.status_code == 200 and detail.json()['burned_video_current'] is True
        assert len(hashes) == 1
        assert str(root) not in summary.text and str(root) not in detail.text
    finally:
        client.close()
