import logging
import os

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models.study import Study
from src.models.trial import Trial
from src.services import exclusion, studies
from src.services.audits import log
from src.utils.serializers import checksum, now, record

QUEUE = 'exclusion-queue'


def _redis():
    return Redis.from_url(os.environ['REDIS_URL'])


def normalize_forced(keys):
    cleaned = []
    for key in keys or []:
        value = str(key).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return sorted(cleaned)


def _snapshot(rows):
    return [
        {'id': row['id'], 'source_key': row['source_key'], 'values': row['values']}
        for row in rows
    ]


def _conflict_payload(error):
    return {
        'first': {
            'source_key': error.first['source_key'],
            'porosity': error.first['porosity'],
            'absorption': error.first['absorption'],
        },
        'second': {
            'source_key': error.second['source_key'],
            'porosity': error.second['porosity'],
            'absorption': error.second['absorption'],
        },
    }


def _summary(trial):
    data = {
        'id': trial.id,
        'study_id': trial.study_id,
        'name': trial.name,
        'status': trial.status,
        'forced_keys': trial.forced_keys,
        'basis_checksum': trial.basis_checksum,
        'created_at': trial.created_at,
        'error': trial.error or '',
        'basis_row_count': len((trial.basis or {}).get('rows', [])),
        'retained_count': None,
        'excluded_count': None,
        'must_exclude_count': None,
    }
    if trial.result:
        data['retained_count'] = trial.result.get('retained_count')
        data['excluded_count'] = trial.result.get('excluded_count')
        data['must_exclude_count'] = len(trial.result.get('must_exclude_keys') or [])
    return data


def _detail(trial):
    data = record(trial)
    data.pop('basis', None)
    data['basis_row_count'] = len((trial.basis or {}).get('rows', []))
    data['basis_frozen'] = bool(trial.basis)
    return data


def list_trials(db, study_id=None):
    query = select(Trial).order_by(Trial.id.desc())
    if study_id:
        query = query.where(Trial.study_id == study_id)
    return [_summary(trial) for trial in db.scalars(query)]


def get(db, id):
    trial = db.get(Trial, id)
    if not trial:
        raise HTTPException(404, '试算记录不存在')
    return _detail(trial)


def preview(db, data):
    studies.require(db, data.study_id)
    rows = studies.observations(db, data.study_id)
    if not rows:
        raise HTTPException(422, '先录入至少一条业务观测')
    forced = normalize_forced(data.forced_keys)
    snapshot = _snapshot(rows)
    checksum_value = exclusion.input_checksum(data.study_id, snapshot, forced)
    try:
        plan = exclusion.build_plan(snapshot, forced)
    except exclusion.UnknownSpecimens as error:
        raise HTTPException(422, str(error))
    except exclusion.ForcedConflict as error:
        return {
            'status': 'conflict',
            'message': str(error),
            'conflict': _conflict_payload(error),
            'forced_keys': forced,
            'basis_checksum': checksum_value,
            'row_count': len(snapshot),
        }
    plan.update({
        'study_id': data.study_id,
        'forced_keys': forced,
        'basis_checksum': checksum_value,
        'row_count': len(snapshot),
    })
    return plan


def create(db, data, user):
    forced = normalize_forced(data.forced_keys)
    # Lock the batch row so a concurrent observation edit cannot slip between
    # the basis check and the frozen snapshot. Observation writes take the
    # same lock (see studies.save_observation).
    study = db.scalar(select(Study).where(Study.id == data.study_id).with_for_update())
    if not study:
        raise HTTPException(404, '业务批次不存在')

    # A retry with the same basis returns the earlier trial instead of
    # creating a duplicate study. The unique (study_id, basis_checksum)
    # constraint is the final guard for concurrent retries.
    existing = db.scalar(
        select(Trial)
        .where(Trial.study_id == data.study_id, Trial.basis_checksum == data.basis_checksum)
        .with_for_update()
    )
    if existing:
        if list(existing.forced_keys or []) != forced:
            raise HTTPException(409, '方案依据与必留项不一致，请重新试算后再确认')
        return {'trial': _summary(existing), 'reused': True}

    rows = studies.observations(db, data.study_id)
    if not rows:
        raise HTTPException(422, '先录入至少一条业务观测')
    snapshot = _snapshot(rows)
    basis = exclusion.basis_payload(data.study_id, snapshot, forced)
    current = checksum(basis)
    if current != data.basis_checksum:
        raise HTTPException(409, '试算依据已变化（观测测值或必留项被修改），旧方案不得生效，请重新试算')

    try:
        plan = exclusion.build_plan(snapshot, forced)
    except exclusion.UnknownSpecimens as error:
        raise HTTPException(422, str(error))
    except exclusion.ForcedConflict as error:
        raise HTTPException(409, str(error))

    trial = Trial(
        study_id=data.study_id,
        name=data.name,
        status='queued',
        forced_keys=forced,
        basis=basis,
        basis_checksum=current,
        frozen={
            'algorithm': exclusion.ALGORITHM_VERSION,
            'basis_checksum': current,
            'retained_keys': plan['retained_keys'],
            'excluded_keys': plan['excluded_keys'],
            'must_exclude_keys': plan['must_exclude_keys'],
        },
        created_at=now(),
    )
    db.add(trial)
    try:
        db.flush()
        log(db, user, 'exclusion.queue', f'{trial.id}: {data.name}')
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Trial).where(
                Trial.study_id == data.study_id,
                Trial.basis_checksum == data.basis_checksum,
            )
        )
        if existing:
            return {'trial': _summary(existing), 'reused': True}
        raise HTTPException(409, '同一方案正在创建，请稍后重试')

    # The DB row is the source of truth. If Redis is unavailable the job is
    # still queued in the database and the worker's recovery sweep will pick
    # it up; the request itself must not fail after the row is committed.
    try:
        _redis().rpush(QUEUE, trial.id)
    except Exception:
        logging.exception('failed to enqueue exclusion trial %s', trial.id)
    return {'trial': _summary(trial), 'reused': False}
