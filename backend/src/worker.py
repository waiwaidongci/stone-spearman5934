import json
import logging
import os
import time
from pathlib import Path

from redis import Redis
from sqlalchemy import select

from src.config.database import Session
from src.models.run import Run
from src.models.trial import Trial
from src.models import user, study, observation, review, audit, trial as trial_model  # noqa: F401 - register tables
from src.services.algorithm import calculate
from src.services.exclusion import build_plan
from src.utils.serializers import checksum

redis = Redis.from_url(os.environ['REDIS_URL'])
ARTIFACTS = Path(os.environ.get('ARTIFACTS_DIR', '/app/artifacts'))
ARTIFACTS.mkdir(parents=True, exist_ok=True)
QUEUES = ('analysis-queue', 'exclusion-queue')


def process_run(db, run_id):
    run = db.get(Run, run_id)
    if not run or run.status in ('completed', 'failed'):
        return False
    run.status = 'running'
    db.commit()
    try:
        if checksum(run.snapshot) != run.checksum:
            raise ValueError('输入快照校验失败')
        result = calculate(run.snapshot, run.parameters)
        result['input_sha256'] = run.checksum
        run.result = result
        run.status = 'completed'
        (ARTIFACTS / f'run-{run_id}.json').write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        )
    except Exception as error:
        logging.exception('analysis failed')
        run.status = 'failed'
        run.error = str(error)
    db.commit()
    return True


def process_trial(db, trial_id):
    trial = db.get(Trial, trial_id)
    if not trial or trial.status in ('completed', 'failed'):
        return False
    trial.status = 'running'
    db.commit()
    try:
        if checksum(trial.basis) != trial.basis_checksum:
            raise ValueError('冻结依据校验失败')
        # Re-derive the basis fields to be safe against partial writes, then
        # compute against the frozen snapshot only; live observations are
        # never read here, so later edits cannot change a queued plan.
        plan = build_plan(trial.basis['rows'], trial.forced_keys or [])
        frozen = trial.frozen or {}
        if frozen.get('retained_keys') != plan['retained_keys']:
            raise ValueError('冻结保留样本与依据不一致')
        plan.update({
            'status': 'optimal',
            'frozen_at': trial.created_at,
            'input_sha256': trial.basis_checksum,
            'basis_algorithm': trial.basis.get('algorithm'),
            'study_id': trial.study_id,
            'name': trial.name,
            'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        })
        trial.result = plan
        trial.status = 'completed'
        (ARTIFACTS / f'exclusion-{trial_id}.json').write_text(
            json.dumps(plan, ensure_ascii=False, allow_nan=False)
        )
    except Exception as error:
        logging.exception('exclusion trial failed')
        trial.status = 'failed'
        trial.error = str(error)
    db.commit()
    return True


PROCESSORS = {'analysis-queue': (Run, process_run), 'exclusion-queue': (Trial, process_trial)}


def recover():
    """Re-queue DB jobs whose Redis entry was lost to a crash.

    Re-adding is safe: creators insert the DB row first and a job that is
    already completed or failed is skipped on dequeue, so a retry never
    creates a second trial or run.
    """
    with Session() as db:
        for queue, (model, _) in PROCESSORS.items():
            for job in db.scalars(select(model).where(model.status.in_(['queued', 'running']))):
                redis.rpush(queue, job.id)


if __name__ == '__main__':
    recover()
    last_sweep = time.monotonic()
    while True:
        Path('/tmp/worker-heartbeat').touch()
        job = redis.blpop(list(QUEUES), timeout=2)
        if job:
            queue = job[0].decode() if isinstance(job[0], bytes) else job[0]
            raw_id = int(job[1])
            _, processor = PROCESSORS[queue]
            with Session() as db:
                processor(db, raw_id)
        # Redis is not durable across a flush/restart; the database is. Sweep
        # it periodically so queued work resumes even without a worker restart.
        if time.monotonic() - last_sweep > 30:
            recover()
            last_sweep = time.monotonic()
