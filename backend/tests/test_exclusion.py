import itertools
import random

import pytest

from src.services import exclusion


def plan(rows, forced=()):
    return exclusion.build_plan(rows, forced)


def make(key, porosity, absorption):
    return {'source_key': key, 'values': {'porosity': porosity, 'absorption': absorption}}


def brute(rows, forced):
    keys = {row['source_key'] for row in rows}
    assert set(forced) <= keys
    best, keeps = -1, []
    for size in range(len(rows) + 1):
        for pick in itertools.combinations(rows, size):
            kept = [row for row in pick]
            if not set(forced) <= {row['source_key'] for row in kept}:
                continue
            ordered = sorted(
                kept,
                key=lambda row: (row['values']['porosity'], row['values']['absorption'], row['source_key']),
            )
            if any(
                a['values']['porosity'] < b['values']['porosity']
                and a['values']['absorption'] > b['values']['absorption']
                for a, b in zip(ordered, ordered[1:])
            ):
                continue
            if size > best:
                best, keeps = size, [{row['source_key'] for row in kept}]
            elif size == best:
                keeps.append({row['source_key'] for row in kept})
    return best, keeps


def brute_must(rows, keeps):
    # Specimens that appear in no optimal solution at all must be excluded.
    ever_kept = set().union(*keeps) if keeps else set()
    return {row['source_key'] for row in rows} - ever_kept


@pytest.mark.parametrize('seed', range(60))
def test_matches_bruteforce(seed):
    rng = random.Random(seed)
    size = rng.randint(1, 8)
    rows = [
        make(f'K{i:02}', rng.randint(1, 4), rng.randint(1, 4))
        for i in range(size)
    ]
    forced = rng.sample([row['source_key'] for row in rows], rng.randint(0, min(2, size)))
    try:
        result = plan(rows, forced)
    except exclusion.ForcedConflict:
        best, keeps = brute(rows, forced)
        with pytest.raises(exclusion.ForcedConflict):
            plan(rows, forced)
        # Confirm no valid subset exists at all (consistent with conflict).
        assert keeps == []
        return
    best, keeps = brute(rows, forced)
    assert result['retained_count'] == best
    assert len(result['retained_keys']) == best
    assert result['excluded_count'] == size - best
    assert set(result['must_exclude_keys']) == brute_must(rows, keeps)
    kept = set(result['retained_keys'])
    assert set(forced) <= kept
    ordered = sorted(
        (row for row in rows if row['source_key'] in kept),
        key=lambda row: (row['values']['porosity'], row['values']['absorption'], row['source_key']),
    )
    assert all(
        not (a['values']['porosity'] < b['values']['porosity'] and a['values']['absorption'] > b['values']['absorption'])
        for a, b in zip(ordered, ordered[1:])
    )


def test_higher_porosity_never_lower_absorption_rule():
    rows = [
        make('A', 10, 3),
        make('B', 12, 2),
        make('C', 14, 5),
    ]
    result = plan(rows)
    # Two optima of size 2: {A, C} and {B, C}; only C is common to both.
    assert result['retained_keys'] == ['A', 'C']
    assert result['must_exclude_keys'] == []
    assert result['excluded_keys'] == ['B']


def test_all_tied_rows_are_retained():
    rows = [make(f'K{i}', 5, 2) for i in range(4)]
    result = plan(rows)
    assert result['retained_count'] == 4
    assert result['excluded_count'] == 0


def test_ties_are_not_inversions():
    rows = [make('A', 10, 2), make('B', 10, 2), make('C', 10, 2)]
    assert plan(rows)['retained_count'] == 3


def test_forced_conflict_names_both_specimens():
    rows = [make('LOW', 10, 9), make('HIGH', 20, 1), make('MID', 15, 5)]
    with pytest.raises(exclusion.ForcedConflict) as caught:
        plan(rows, ['LOW', 'HIGH'])
    message = str(caught.value)
    assert 'LOW' in message and 'HIGH' in message
    assert caught.value.first['source_key'] == 'LOW'
    assert caught.value.second['source_key'] == 'HIGH'


def test_unknown_forced_key_rejected():
    with pytest.raises(exclusion.UnknownSpecimens):
        plan([make('A', 1, 1)], ['NOPE'])


def test_duplicate_source_keys_rejected():
    with pytest.raises(ValueError):
        plan([make('A', 1, 1), make('A', 2, 2)])


def test_missing_field_rejected():
    with pytest.raises(ValueError):
        plan([{'source_key': 'A', 'values': {'porosity': 1}}])


def test_checksum_tracks_values_and_must_keeps():
    rows = [{'id': 1, 'source_key': 'A', 'values': {'porosity': 1, 'absorption': 2}}]
    base = exclusion.input_checksum(1, rows, [])
    changed = exclusion.input_checksum(1, [{'id': 1, 'source_key': 'A', 'values': {'porosity': 1, 'absorption': 3}}], [])
    assert base != changed
    assert base == exclusion.input_checksum(1, rows, [])
    assert base != exclusion.input_checksum(1, rows, ['A'])
    assert base != exclusion.input_checksum(2, rows, [])


def test_recreated_row_gets_new_basis():
    original = [{'id': 7, 'source_key': 'A', 'values': {'porosity': 1, 'absorption': 2}}]
    recreated = [{'id': 8, 'source_key': 'A', 'values': {'porosity': 1, 'absorption': 2}}]
    assert exclusion.input_checksum(1, original, []) != exclusion.input_checksum(1, recreated, [])


def test_large_input_is_fast():
    import time

    rows = [
        make(f'DEMO-{i:05}', (i * 7919) % 10000 / 100.0, (i * 104729) % 10000 / 100.0)
        for i in range(5000)
    ]
    started = time.perf_counter()
    result = plan(rows)
    elapsed = time.perf_counter() - started
    assert result['retained_count'] >= 1
    assert elapsed < 5
