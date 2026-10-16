from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database import Conflict, DatabaseError
from app.scheduling import Scheduling
from app.store import Store

NOW = '2026-10-16T10:00:00Z'
DUE = '2026-10-16T10:03:00Z'


@pytest.fixture
def scheduling(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    store.initialize()
    return Scheduling(store.db)


def test_restart_and_duplicate_reminder_trigger(scheduling):
    reminder = scheduling.create_reminder('cabinet-01', '吃苹果', DUE, 'r1', ['web', 'voice'], at=NOW)
    assert scheduling.create_reminder('cabinet-01', '吃苹果', DUE, 'r1', ['voice', 'web'], at=DUE) == reminder
    restored = Scheduling(scheduling.db)
    assert restored.trigger_due(DUE)['triggered'] == [reminder['id']]
    assert restored.trigger_due(DUE)['triggered'] == []
    saved = restored.list_reminders('cabinet-01')[0]
    assert len(saved['deliveries']) == 2
    assert all(d['state'] == 'queued' for d in saved['deliveries'])
    with pytest.raises(Conflict):
        scheduling.create_reminder('cabinet-01', '不同提醒', DUE, 'r1', at=NOW)


def test_cancel_and_stale_reminder(scheduling):
    first = scheduling.create_reminder('cabinet-01', '取消', DUE, 'first', at=NOW)
    second = scheduling.create_reminder('cabinet-01', '过期', DUE, 'second', at=NOW)
    assert scheduling.cancel_reminder(first['id'])['status'] == 'canceled'
    result = scheduling.trigger_due('2026-10-16T11:00:00Z')
    assert result == {'triggered': [], 'missed': [second['id']]}


def test_job_lease_retry_and_stale_worker_cannot_finish(scheduling):
    job = scheduling.enqueue('key', 'test', {'x': 1}, at=NOW, max_attempts=2)
    assert scheduling.enqueue('key', 'test', {'x': 1}, at=NOW, max_attempts=2)['id'] == job['id']
    first = scheduling.claim_job(NOW, lease_seconds=10)
    assert scheduling.claim_job(NOW) is None
    second = scheduling.claim_job('2026-10-16T10:00:11Z')
    assert second['attempts'] == 2 and first['lease_token'] != second['lease_token']
    with pytest.raises(Conflict):
        scheduling.finish_job(first['id'], first['lease_token'], at='2026-10-16T10:00:12Z')
    scheduling.finish_job(second['id'], second['lease_token'], error='failed', at='2026-10-16T10:00:12Z')
    assert scheduling.claim_job('2026-10-16T10:05:00Z') is None


def test_concurrent_claim_has_one_owner_and_renewal(scheduling):
    scheduling.enqueue('one', 'test', {}, at=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: scheduling.claim_job(NOW), range(2)))
    assert sum(job is not None for job in claimed) == 1
    job = next(job for job in claimed if job)
    scheduling.renew_lease(job['id'], job['lease_token'], at='2026-10-16T10:00:10Z')
    assert scheduling.finish_job(job['id'], job['lease_token'], at='2026-10-16T10:00:20Z')['state'] == 'done'


def test_channel_ack_never_invents_audio_playback(scheduling):
    scheduling.create_reminder('cabinet-01', '吃苹果', DUE, 'key', ['web', 'voice'], at=NOW)
    scheduling.trigger_due(DUE)
    rows = scheduling.list_reminders('cabinet-01')[0]['deliveries']
    web = next(d for d in rows if d['channel'] == 'web')
    voice = next(d for d in rows if d['channel'] == 'voice')
    with pytest.raises(DatabaseError):
        scheduling.acknowledge(web['id'], 'web', 'played')
    with pytest.raises(DatabaseError):
        scheduling.acknowledge(voice['id'], 'web', 'received')
    assert scheduling.acknowledge(voice['id'], 'voice', 'received')['played_at'] is None
    assert scheduling.acknowledge(voice['id'], 'voice', 'played')['state'] == 'played'
    assert scheduling.acknowledge(voice['id'], 'voice', 'received')['state'] == 'played'


def test_cancel_after_trigger_cancels_only_unclaimed_deliveries(scheduling):
    reminder = scheduling.create_reminder('cabinet-01', '吃苹果', DUE, 'key', ['web', 'voice'], at=NOW)
    scheduling.trigger_due(DUE)
    job = scheduling.claim_job(DUE, kinds=['reminder.deliver'])
    scheduling.cancel_reminder(reminder['id'])
    rows = scheduling.list_reminders('cabinet-01')[0]['deliveries']
    assert sum(d['state'] == 'canceled' for d in rows) == 1
    assert job is not None
