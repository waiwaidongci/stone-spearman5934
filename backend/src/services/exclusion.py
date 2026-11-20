"""Minimal-exclusion trial for anomalous specimens.

The retained sample must satisfy one business rule: a specimen with higher
porosity may not have lower absorption than another retained specimen.  After
ordering specimens by (porosity, absorption, source_key), every valid retained
set is a non-decreasing subsequence of absorption, so the least-exclusion
solution is a longest non-decreasing subsequence (LNDS).

Forced (must-keep) specimens split that order into independent blocks, which
are solved with Fenwick prefix maxima in O(n log n).  No specimen pair is ever
compared, so a 5000-row study stays linear-ish rather than quadratic.
"""

import math

from src.utils.serializers import checksum

ALGORITHM_VERSION = 'exclusion-monotone-nondecreasing-v1'
NEG_INF = float('-inf')
POS_INF = float('inf')


class UnknownSpecimens(ValueError):
    def __init__(self, keys):
        self.keys = list(keys)
        super().__init__('必留试件不存在：' + '、'.join(self.keys))


class ForcedConflict(ValueError):
    """Raised when the must-keep specimens contradict the monotone rule."""

    def __init__(self, first, second):
        self.first = first
        self.second = second
        super().__init__(
            '必留项矛盾：{a}（孔隙率 {ap:g}，吸水率 {aa:g}）与 {b}（孔隙率 {bp:g}，'
            '吸水率 {ba:g}）孔隙率更高却吸水率更低，无法同时保留。'.format(
                a=first['source_key'], ap=first['porosity'], aa=first['absorption'],
                b=second['source_key'], bp=second['porosity'], ba=second['absorption'],
            )
        )


class _Fenwick:
    """Prefix maximum over (length, earliest owner index)."""

    def __init__(self, size):
        self.size = size
        self.length = [0] * (size + 1)
        self.owner = [-1] * (size + 1)

    def query(self, i):
        best_len, best_owner = 0, -1
        while i > 0:
            length, owner = self.length[i], self.owner[i]
            if length > best_len or (
                length == best_len
                and length > 0
                and owner != -1
                and (best_owner == -1 or owner < best_owner)
            ):
                best_len, best_owner = length, owner
            i -= i & -i
        return best_len, best_owner

    def update(self, i, length, owner):
        while i <= self.size:
            if length > self.length[i] or (
                length == self.length[i]
                and length > 0
                and (self.owner[i] == -1 or owner < self.owner[i])
            ):
                self.length[i], self.owner[i] = length, owner
            i += i & -i


def _item(row):
    values = row.get('values') or {}
    if 'porosity' not in values or 'absorption' not in values:
        raise ValueError('观测字段不完整')
    porosity, absorption = float(values['porosity']), float(values['absorption'])
    if not math.isfinite(porosity) or not math.isfinite(absorption):
        raise ValueError('观测必须为有限数值')
    key = row.get('source_key')
    if not isinstance(key, str) or not key:
        raise ValueError('观测缺少源记录编号')
    return {'source_key': key, 'porosity': porosity, 'absorption': absorption}


def _block(ordered, indices, low, high):
    """Longest non-decreasing run inside one forced-anchor block."""
    members = [j for j in indices if low <= ordered[j]['absorption'] <= high]
    if not members:
        return {'length': 0, 'chain': [], 'on': set()}
    values = [ordered[j]['absorption'] for j in members]
    levels = sorted(set(values))
    rank = {v: i + 1 for i, v in enumerate(levels)}
    ranks = [rank[v] for v in values]
    size, count = len(levels), len(members)

    forward = _Fenwick(size)
    ending = [1] * count
    previous = [-1] * count
    for t in range(count):
        length, owner = forward.query(ranks[t])
        ending[t], previous[t] = length + 1, owner
        forward.update(ranks[t], ending[t], t)

    backward = _Fenwick(size)
    starting = [1] * count
    following = [-1] * count
    for t in range(count - 1, -1, -1):
        mirrored = size - ranks[t] + 1
        length, owner = backward.query(mirrored)
        starting[t], following[t] = length + 1, owner
        backward.update(mirrored, starting[t], t)

    longest = max(ending)
    on_optimum = {
        t for t in range(count) if ending[t] + starting[t] - 1 == longest
    }
    end = next(t for t in range(count) if ending[t] == longest)
    chain, cursor = [], end
    while cursor != -1:
        chain.append(members[cursor])
        cursor = previous[cursor]
    chain.reverse()
    return {'length': longest, 'chain': chain, 'on': {members[t] for t in on_optimum}}


def _conflict(ordered, forced_positions):
    strongest = None
    for position in forced_positions:
        item = ordered[position]
        if strongest is not None and item['absorption'] < strongest['absorption']:
            return strongest, item
        if strongest is None or item['absorption'] > strongest['absorption']:
            strongest = item
    return None


def build_plan(rows, forced_keys):
    """Return the least-exclusion plan and the specimens no optimum can keep.

    ``rows`` hold ``source_key`` and ``values`` (porosity, absorption).
    ``forced_keys`` must all exist in ``rows``.
    """
    items = [_item(row) for row in rows]
    keys = [item['source_key'] for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError('源记录编号重复，无法试算')
    forced = sorted({key for key in forced_keys})
    missing = [key for key in forced if key not in set(keys)]
    if missing:
        raise UnknownSpecimens(missing)

    ordered = sorted(items, key=lambda x: (x['porosity'], x['absorption'], x['source_key']))
    position = {item['source_key']: i for i, item in enumerate(ordered)}
    forced_positions = sorted(position[key] for key in forced)

    conflict = _conflict(ordered, forced_positions)
    if conflict:
        raise ForcedConflict(*conflict)

    blocks, cursor = [], -1
    for forced_position in forced_positions:
        blocks.append((
            range(cursor + 1, forced_position),
            NEG_INF if cursor < 0 else ordered[cursor]['absorption'],
            ordered[forced_position]['absorption'],
        ))
        cursor = forced_position
    blocks.append((
        range(cursor + 1, len(ordered)),
        NEG_INF if cursor < 0 else ordered[cursor]['absorption'],
        POS_INF,
    ))

    retained = set(forced)
    must_exclude = set()
    block_length = 0
    for indices, low, high in blocks:
        solution = _block(ordered, indices, low, high)
        block_length += solution['length']
        retained.update(ordered[j]['source_key'] for j in solution['chain'])
        for j in indices:
            if j in solution['on']:
                continue
            key = ordered[j]['source_key']
            if key not in forced:
                must_exclude.add(key)

    chosen = [item for item in ordered if item['source_key'] in retained]
    excluded = [item for item in ordered if item['source_key'] not in retained]
    return {
        'algorithm': ALGORITHM_VERSION,
        'status': 'optimal',
        'total': len(ordered),
        'retained_count': len(retained),
        'excluded_count': len(excluded),
        'minimum_excluded': len(excluded),
        'longest_chain': block_length + len(forced),
        'forced_keys': forced,
        'retained_keys': [item['source_key'] for item in chosen],
        'excluded_keys': [item['source_key'] for item in excluded],
        'must_exclude_keys': [item['source_key'] for item in ordered if item['source_key'] in must_exclude],
        'retained': [dict(item, forced=item['source_key'] in forced) for item in chosen],
        'excluded': [dict(item) for item in excluded],
        'must_exclude': [dict(item) for item in ordered if item['source_key'] in must_exclude],
        'flexible': [
            dict(item) for item in ordered
            if item['source_key'] not in retained and item['source_key'] not in must_exclude
        ],
    }


def basis_payload(study_id, snapshot, forced_keys):
    """Canonical frozen basis: study, algorithm version, must-keeps and rows.

    ``snapshot`` entries keep their observation id, so deleting and re-adding a
    source key still changes the fingerprint. The retained sample is not part
    of the basis because it is a deterministic function of it.
    """
    return {
        'algorithm': ALGORITHM_VERSION,
        'study_id': study_id,
        'forced_keys': sorted({key for key in forced_keys}),
        'rows': [
            {'id': row.get('id'), 'source_key': row['source_key'], 'values': row['values']}
            for row in snapshot
        ],
    }


def input_checksum(study_id, snapshot, forced_keys):
    return checksum(basis_payload(study_id, snapshot, forced_keys))
