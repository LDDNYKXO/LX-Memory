"""Explicit cloud-record replacement metadata. Data only; no commands or write operations."""
import json
from datetime import datetime
from .project_identity import normalize_name

PREFIX = '<!-- memory-project-record '


def parse_record(content, name_keys):
    lines = content.lstrip().splitlines() if isinstance(content, str) else []
    line = lines[0] if lines else ''
    if len(line) > 2048 or not line.startswith(PREFIX) or not line.endswith(' -->'):
        return None
    try:
        value = json.loads(line[len(PREFIX):-4])
        if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
            return None
        if normalize_name(value.get('project')) not in name_keys:
            return None
        paths = value['supersedes']
        if not isinstance(paths, list) or len(paths) > 4 or len(set(paths)) != len(paths):
            return None
        for path in paths:
            if (not isinstance(path, str) or len(path) > 512 or
                not path.startswith(('wiki/projects/', 'projects/')) or
                any(part in ('', '.', '..') for part in path.split('/')) or
                any(c in path for c in ('?', '#', '\\'))):
                return None
        stamp = datetime.fromisoformat(value['as_of'])
        if stamp.tzinfo is None:
            return None
        return {k: value[k] for k in ('schema', 'project', 'supersedes', 'as_of')}
    except (ValueError, KeyError, TypeError):
        return None


def record_policy(content, name_keys, now):
    """Interpret explicit record applicability, never infer dates or kind from prose.

    An interval describes this record's applicability, not when a task is due and not
    whether its claims have been independently verified. ``as_of`` remains separate.
    """
    result = {'record_kind': 'unknown', 'time_state': 'undeclared', 'as_of': None,
              'valid_from': None, 'valid_until': None, 'reason': 'legacy_unmarked'}
    if not isinstance(content, str) or not content.lstrip().startswith(PREFIX):
        return result
    marker = parse_record(content, name_keys)
    if marker is None:
        return {**result, 'time_state': 'invalid', 'reason': 'invalid_or_foreign_declaration'}
    result['as_of'] = marker['as_of']
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('evaluation_time_requires_timezone')
        if datetime.fromisoformat(marker['as_of']) > now:
            raise ValueError('future_as_of')
        line = content.lstrip().splitlines()[0]
        value = json.loads(line[len(PREFIX):-4])
        kind = value.get('record_kind', 'unknown')
        if 'record_kind' in value and kind not in ('handoff', 'history', 'test'):
            raise ValueError('invalid_record_kind')
        result['record_kind'] = kind
        dates = {}
        for key in ('valid_from', 'valid_until'):
            if key in value:
                if not isinstance(value[key], str):
                    raise ValueError('invalid_' + key)
                stamp = datetime.fromisoformat(value[key])
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError('timezone_required_' + key)
                dates[key] = stamp
                result[key] = value[key]
        start, end = dates.get('valid_from'), dates.get('valid_until')
        if start and end and start >= end:
            raise ValueError('invalid_interval')
        state = ('future' if start and now < start else 'expired' if end and now >= end
                 else 'in_range' if dates else 'undeclared')
        return {**result, 'time_state': state, 'reason': state}
    except (ValueError, TypeError) as error:
        return {**result, 'time_state': 'invalid', 'reason': str(error)[:100]}


def resolve_replacements(contents, name_keys):
    """Resolve only the finite returned candidate set, without fetching referenced paths.

    Each edge is an explicit same-project claim, never an inference from a score/date.
    Missing markers are permitted on legacy leaves; an invalid/foreign marker is not.
    """
    records = {}
    for path, content in contents.items():
        marker = parse_record(content, name_keys)
        if isinstance(content, str) and content.lstrip().startswith(PREFIX) and marker is None:
            return {'state': 'project_ambiguous', 'reason': '候选项目声明不匹配或替代元数据无效。'}
        records[path] = marker
    graph = {path: set(marker['supersedes']) & contents.keys() if marker else set()
             for path, marker in records.items()}
    visiting, visited = set(), set()

    def cyclic(path):
        if path in visiting:
            return True
        if path in visited:
            return False
        visiting.add(path)
        if any(cyclic(child) for child in graph[path]):
            return True
        visiting.remove(path)
        visited.add(path)
        return False

    if any(cyclic(path) for path in graph):
        return {'state': 'conflict', 'reason': '明确替代关系存在循环，未选取当前记录。'}
    replaced = set().union(*graph.values()) if graph else set()
    roots = set(graph) - replaced
    # An unrelated root must not hide competing final replacements in another
    # component. Only roots compete: a later explicit merge covering both is valid.
    root_coverage = []
    for root in roots:
        covered, pending = set(), list(graph[root])
        while pending:
            child = pending.pop()
            if child not in covered:
                covered.add(child)
                pending.extend(graph[child])
        if any(covered & previous for previous in root_coverage):
            return {'state': 'conflict', 'reason': '候选子图存在竞争替代，不能被其它独立记录或用途过滤隐藏。'}
        root_coverage.append(covered)
    if len(roots) == 1:
        winner = next(iter(roots))
        covered, pending = set(), list(graph[winner])
        while pending:
            child = pending.pop()
            if child not in covered:
                covered.add(child)
                pending.extend(graph[child])
        if records[winner] and covered == set(graph) - {winner}:
            return {'state': 'ok', 'selected': winner, 'superseded': sorted(covered)}
    if len(roots) > 1 and replaced and all(records[path] and graph[path] for path in roots):
        return {'state': 'conflict', 'reason': '多个记录竞争替代旧版本，缺少明确的最终替代关系。'}
    return {'state': 'project_ambiguous', 'reason': '明确替代链未覆盖全部已返回候选，未任选当前记录。'}
