import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_FILE = Path(tempfile.mkdtemp()) / 'test.db'
os.environ['DATABASE_URL'] = f'sqlite:///{DB_FILE}'
# Isolated per-run database: never reuse the project data volume.
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACTS_DIR'] = tempfile.mkdtemp()

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from src.config import database  # noqa: E402

# The default engine is built from DATABASE_URL at import time; point it at a
# fresh file database created for this test session.
database.engine = create_engine(os.environ['DATABASE_URL'])
database.Session.configure(bind=database.engine)

from src.main import app  # noqa: E402
from src.models import audit, observation, review, run, study, trial, user  # noqa: E402
from src.config.database import Base, Session  # noqa: E402
from src.models.study import Study  # noqa: E402
from src.models.observation import Observation  # noqa: E402
from src.models.trial import Trial  # noqa: E402
from src.models.user import User  # noqa: E402
from src.services import exclusions  # noqa: E402
from src.utils.security import hash_password, token  # noqa: E402
import src.worker as worker  # noqa: E402


@pytest.fixture(scope='session')
def client():
    Base.metadata.create_all(database.engine)
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=f'{role}@demo.local', role=role, password_hash=hash_password('x')))
        db.commit()
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server)
    import src.middlewares.rate_limit as rate_limit
    import src.services.exclusions as exclusions_service
    rate_limit.redis = fake
    exclusions_service._redis = lambda: fake
    worker.redis = fake
    worker.ARTIFACTS = Path(tempfile.mkdtemp())
    app.state.redis = fake
    with TestClient(app) as test_client:
        test_client.redis = fake
        yield test_client


@pytest.fixture(autouse=True)
def clean(client):
    with Session() as session:
        session.query(Trial).delete()
        session.commit()
    client.redis.flushall()
    yield


@pytest.fixture()
def db(client):
    with Session() as session:
        yield session


@pytest.fixture()
def make_study(db):
    import uuid

    def factory(rows):
        study = Study(name=f'批次-{uuid.uuid4().hex[:10]}', source_filename='f.csv')
        db.add(study)
        db.flush()
        for key, porosity, absorption in rows:
            db.add(Observation(study_id=study.id, source_key=key,
                               values={'porosity': porosity, 'absorption': absorption}))
        db.commit()
        return study.id

    return factory


def headers(client, role='analyst'):
    with Session() as db:
        user = db.query(User).filter(User.email == f'{role}@demo.local').one()
        return {'Authorization': 'Bearer ' + token(user)}


def rows(client, study_id):
    return client.get(f'/studies/{study_id}/observations',
                      headers=headers(client)).json()


def preview(client, study_id, forced=()):
    return client.post('/exclusions/preview',
                       json={'study_id': study_id, 'forced_keys': list(forced)},
                       headers=headers(client))


def confirm(client, study_id, checksum_value, name='最少剔除方案', forced=()):
    return client.post('/exclusions', json={
        'study_id': study_id,
        'name': name,
        'framed': True,
        'forced_keys': list(forced),
        'basis_checksum': checksum_value,
    }, headers=headers(client))


def test_preview_reports_optimum_and_must_exclude(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    response = preview(client, study_id)
    assert response.status_code == 200
    body = response.json()
    assert body['retained_keys'] == ['A', 'C']
    assert body['excluded_keys'] == ['B']
    assert body['must_exclude_keys'] == []


def test_preview_conflict_names_the_pair(client, make_study):
    study_id = make_study([('LOW', 10, 9), ('HIGH', 20, 1), ('MID', 15, 5)])
    body = preview(client, study_id, ['LOW', 'HIGH']).json()
    assert body['status'] == 'conflict'
    assert {body['conflict']['first']['source_key'], body['conflict']['second']['source_key']} == {'LOW', 'HIGH'}
    assert 'LOW' in body['message'] and 'HIGH' in body['message']


def test_preview_unknown_forced_key(client, make_study):
    study_id = make_study([('A', 10, 3)])
    response = preview(client, study_id, ['NOPE'])
    assert response.status_code == 422
    assert 'NOPE' in response.json()['detail']


def test_confirmation_with_stale_basis_is_rejected(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    observation = rows(client, study_id)[0]
    client.put(f'/studies/{study_id}/observations/{observation["id"]}',
               json={'source_key': observation['source_key'],
                     'values': {'porosity': 10, 'absorption': 4}},
               headers=headers(client))
    response = confirm(client, study_id, checksum_value)
    assert response.status_code == 409
    assert '旧方案不得生效' in response.json()['detail']
    with Session() as db:
        assert db.query(Trial).count() == 0


def test_confirmation_with_changed_forced_keys_is_rejected(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    checksum_value = preview(client, study_id, ['A']).json()['basis_checksum']
    response = confirm(client, study_id, checksum_value, forced=['C'])
    assert response.status_code == 409


def test_retry_reuses_same_trial_and_freezes_basis(client, make_study, db):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    checksum_value = preview(client, study_id, ['C']).json()['basis_checksum']
    first = confirm(client, study_id, checksum_value, forced=['C'])
    assert first.status_code == 200
    assert first.json()['reused'] is False
    second = confirm(client, study_id, checksum_value, forced=['C'])
    assert second.status_code == 200
    assert second.json()['reused'] is True
    assert second.json()['trial']['id'] == first.json()['trial']['id']
    assert db.query(Trial).count() == 1
    trial = db.query(Trial).one()
    assert trial.status == 'queued'
    assert trial.basis['forced_keys'] == ['C']
    assert len(trial.basis['rows']) == 3
    assert trial.result is None


def test_result_uses_frozen_basis_after_later_edits(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    trial_id = confirm(client, study_id, checksum_value).json()['trial']['id']
    # Edit the live source data while the trial sits in the queue.
    observation = rows(client, study_id)[0]
    client.put(f'/studies/{study_id}/observations/{observation["id"]}',
               json={'source_key': observation['source_key'],
                     'values': {'porosity': 10, 'absorption': 0}},
               headers=headers(client))
    with Session() as db:
        assert worker.process_trial(db, trial_id)
        saved = db.get(Trial, trial_id)
        assert saved.status == 'completed'
        assert saved.result['retained_keys'] == ['A', 'C']
        probe = {item['source_key']: item['absorption'] for item in saved.result['retained']}
        assert probe['A'] == 3
        assert saved.result['input_sha256'] == checksum_value
    # A second delivery of the same job is a no-op.
    with Session() as db:
        assert worker.process_trial(db, trial_id) is False
        assert db.query(Trial).count() == 1


def test_recovery_requeues_without_duplicating(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    trial_id = confirm(client, study_id, checksum_value).json()['trial']['id']
    client.redis.delete('exclusion-queue')
    worker.recover()
    queued = client.redis.lrange('exclusion-queue', 0, -1)
    assert [int(x) for x in queued] == [trial_id]
    with Session() as db:
        assert worker.process_trial(db, trial_id)
        assert worker.process_trial(db, trial_id) is False
        assert db.query(Trial).count() == 1


def test_viewer_cannot_confirm_but_can_preview(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    response = client.post('/exclusions', json={
        'study_id': study_id, 'name': '方案', 'forced_keys': [],
        'basis_checksum': checksum_value,
    }, headers=headers(client, 'viewer'))
    assert response.status_code == 403
    assert client.post('/exclusions/preview',
                       json={'study_id': study_id, 'forced_keys': []},
                       headers=headers(client, 'viewer')).status_code == 200
    listing = client.get('/exclusions', headers=headers(client, 'viewer'))
    assert listing.status_code == 200


def test_conflict_cannot_be_confirmed(client, make_study):
    study_id = make_study([('LOW', 10, 9), ('HIGH', 20, 1)])
    body = preview(client, study_id, ['LOW', 'HIGH']).json()
    response = confirm(client, study_id, body['basis_checksum'], forced=['LOW', 'HIGH'])
    assert response.status_code == 409
    with Session() as db:
        assert db.query(Trial).count() == 0


def test_delete_and_recreate_invalidates_old_basis(client, make_study):
    study_id = make_study([('A', 10, 3), ('B', 12, 2), ('C', 14, 5)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    observation = rows(client, study_id)[0]
    client.delete(f'/studies/{study_id}/observations/{observation["id"]}',
                  headers=headers(client))
    client.post(f'/studies/{study_id}/observations',
                json={'source_key': observation['source_key'], 'values': observation['values']},
                headers=headers(client))
    response = confirm(client, study_id, checksum_value)
    assert response.status_code == 409
    with Session() as db:
        assert db.query(Trial).count() == 0


def test_failed_trial_keeps_error_and_is_not_retried(client, make_study, db):
    study_id = make_study([('A', 10, 3), ('B', 12, 2)])
    checksum_value = preview(client, study_id).json()['basis_checksum']
    trial_id = confirm(client, study_id, checksum_value).json()['trial']['id']
    stored = db.get(Trial, trial_id)
    stored.basis = {'algorithm': stored.basis['algorithm'], 'study_id': stored.study_id,
                    'forced_keys': [], 'rows': []}
    db.commit()
    with Session() as session:
        assert worker.process_trial(session, trial_id)
        assert session.get(Trial, trial_id).status == 'failed'
        assert '冻结依据校验失败' in session.get(Trial, trial_id).error
        assert worker.process_trial(session, trial_id) is False


def test_unknown_study_and_empty_batch(client):
    assert preview(client, 999999).status_code == 404
    with Session() as db:
        study = Study(name='空批次-x', source_filename='f.csv')
        db.add(study)
        db.commit()
        empty_id = study.id
    assert preview(client, empty_id).status_code == 422
