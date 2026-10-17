"""Pure presentation of a raw context; audit never competes for essential space."""
import copy
import json

ESSENTIAL_FIELDS = ['project', 'goal', 'progress.current', 'progress.pending_verification',
                    'next_step', 'constraints', 'sources', 'field_sources', 'unknowns', 'conflicts']


def result_envelope(data):
    """The text-only MCP result, including nested JSON escaping (no JSON-RPC framing)."""
    return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
            'isError': False, 'resultType': 'complete'}


def output_chars(data):
    return len(json.dumps(result_envelope(data), ensure_ascii=False))


def measure(data):
    for _ in range(8):
        size = output_chars(data)
        if data['budget']['output_chars'] == size:
            return size
        data['budget']['output_chars'] = size
    raise ValueError('output size did not settle')


def background(raw, limit=8000, audit=None):
    if raw.get('response_schema') == 2:
        result = copy.deepcopy(raw)
        if audit is not None:
            result['audit'] = audit
    else:
        # `sources` bodies (up to CONTEXT_TOTAL_CHARS) are rebuilt as metadata below; copying
        # them here would duplicate the largest part of the evidence graph and discard it.
        result = {k: copy.deepcopy(v) for k, v in raw.items()
                  if not k.startswith('_') and k not in ('sources', 'record')}
        project = raw.get('project', {})
        result['project'] = {k: copy.deepcopy(project[k]) for k in
                             ('selected', 'title', 'evidence', 'policy_evaluated_at', 'policy_recheck_required')
                             if k in project}
        # Ambiguous candidates are necessary to let the user identify the project.
        if raw['state'] == 'project_ambiguous':
            result['project']['candidates'] = copy.deepcopy(project.get('candidates', []))
        result['progress']['executed'] = []
        result['field_sources'].pop('progress.executed', None)
        paths = {project.get('selected')}
        for origin in result['field_sources'].values():
            paths.add(origin.get('path'))
            paths.update(x.get('path') for x in origin.get('origins', []))
        result['sources'] = [{k: copy.deepcopy(source[k]) for k in
                              ('path', 'source', 'as_of', 'updated', 'retrieved_at') if k in source}
                             for source in raw.get('sources', [])
                             if source.get('path') in paths or str(source.get('path', '')).startswith('core/')]
        record = raw.get('record', {})
        for source in result['sources']:
            if source['path'] == project.get('selected') and record.get('as_of'):
                source['as_of'] = record['as_of']
        result.pop('record', None)
        # Only this module's historical exclusion notice is an audit disclosure, not an unknown.
        result['unknowns'] = [x for x in result['unknowns']
                              if not x.startswith('以下页面已被明确取代，未采用：')]
        constraints_status = raw.get('_constraints_status', 'complete' if raw['constraints'] else 'unknown')
        missing = []
        if not project.get('selected'): missing.append('project')
        if not result['goal']: missing.append('goal')
        if not result['progress']['current']: missing.append('progress.current')
        if not result['next_step']: missing.append('next_step')
        if constraints_status != 'complete': missing.append('constraints')
        if missing and result['state'] == 'complete':
            result['state'] = 'partial'
        if constraints_status == 'unknown':
            result['unknowns'].append('约束声明或来源覆盖不完整；不得将缺失边界解释为无约束。')
        result['response_schema'] = 2
        result['essential'] = {'status': 'complete' if result['state'] == 'complete' and not missing else 'partial',
                               'missing_fields': missing, 'constraints_status': constraints_status}
        result['audit'] = audit or {'status': 'unavailable'}
        result['history_delivery'] = 'audit_only'
    if measure(result) <= limit:
        return result
    # Atomic failure: no subset of facts is offered as an actionable background.
    original_state = result['state']
    source_budget = result.get('budget') or {}
    source_consumption = result.get('consumption') or {}
    failed = {
        'response_schema': 2,
        'state': original_state if original_state in ('conflict', 'project_ambiguous', 'service_unavailable') else 'partial',
        'project': {'selected': None}, 'goal': '',
        'progress': {'executed': [], 'current': [], 'pending_verification': []},
        'next_step': '', 'constraints': [], 'sources': [], 'field_sources': {},
        'unknowns': ['当前任务必需背景超过输出预算；不得依赖缺失信息推进或宣称接续成功。'
                     + ('歧义候选未随失败信封返回，请带更明确的 project 或 cwd 重新请求。'
                        if original_state == 'project_ambiguous' else '')],
        'conflicts': [], 'reason': 'essential_budget_exceeded',
        'essential': {'status': 'budget_exceeded', 'missing_fields': ESSENTIAL_FIELDS.copy(),
                      'constraints_status': 'budget_exceeded'},
        'audit': result['audit'],
        'history_delivery': 'audit_only',
        'consumption': {'background_sources': None,
                        'filesystem_background_fallback': source_consumption.get('filesystem_background_fallback'),
                        'extra_search_fallback': source_consumption.get('extra_search_fallback'),
                        'latest_state_verified': False,
                        'enforcement': source_consumption.get('enforcement')},
        'budget': {'search_attempts': source_budget.get('search_attempts'),
                   'reads': source_budget.get('reads'),
                   'chars': source_budget.get('chars'),
                   'replacement_reads': source_budget.get('replacement_reads'),
                   'output_chars': 0, 'truncated': True}}
    if 'error' in result:
        failed['error'] = {'code': result['error']['code']}
    if measure(failed) > limit:
        raise ValueError('output budget smaller than fixed failure envelope')
    return failed
