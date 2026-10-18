"""Pure synthetic record declarations; never accesses any memory service."""
from datetime import datetime
import json
import unittest
from lingxi_connector import project_record


def body(**extra):
    value={'schema':1,'project':'Example','supersedes':[],'as_of':'2026-10-16T12:12:00+08:00',**extra}
    return '<!-- memory-project-record '+json.dumps(value,ensure_ascii=False)+' -->\n'


class RecordPolicyTests(unittest.TestCase):
    def policy(self, text=None, now='2026-10-16T12:12:00+08:00', **fields):
        self.assertTrue(callable(getattr(project_record,'record_policy',None)), 'record_policy is not implemented')
        return project_record.record_policy(body(**fields) if text is None else text,
                                            ['example'],datetime.fromisoformat(now))

    def test_legacy_marker_keeps_unknown_kind_and_undeclared_time(self):
        p=self.policy()
        self.assertEqual((p['record_kind'],p['time_state']),('unknown','undeclared'))
        self.assertEqual(p['as_of'],'2026-10-16T12:12:00+08:00')
        self.assertIsNone(p['valid_from'])

    def test_unmarked_prose_is_unknown_not_a_guessed_kind(self):
        p=self.policy(text='测试 historical note; deadline 2020-01-01')
        self.assertEqual((p['record_kind'],p['time_state']),('unknown','undeclared'))

    def test_recognized_kinds_do_not_claim_current_truth(self):
        for kind in ('handoff','history','test'):
            self.assertEqual(self.policy(record_kind=kind)['record_kind'],kind)
            self.assertEqual(self.policy(record_kind=kind)['time_state'],'undeclared')

    def test_unknown_kind_is_invalid_instead_of_current(self):
        for kind in ('official',None,True,{},['test']):
            self.assertEqual(self.policy(record_kind=kind)['time_state'],'invalid')

    def test_explicit_interval_is_left_inclusive_and_right_exclusive(self):
        fields={'valid_from':'2026-10-16T12:12:00+08:00','valid_until':'2026-10-16T13:00:00+08:00'}
        self.assertEqual(self.policy(**fields)['time_state'],'in_range')
        self.assertEqual(self.policy(now=fields['valid_until'],**fields)['time_state'],'expired')

    def test_offsets_are_compared_as_instants(self):
        p=self.policy(valid_from='2026-10-16T04:12:00Z',valid_until='2026-10-16T04:13:00Z')
        self.assertEqual(p['time_state'],'in_range')

    def test_future_and_expired_do_not_depend_on_as_of(self):
        self.assertEqual(self.policy(valid_from='2027-01-01T00:00:00Z')['time_state'],'future')
        self.assertEqual(self.policy(valid_until='2025-01-01T00:00:00Z')['time_state'],'expired')

    def test_partial_interval_has_only_declared_bound(self):
        self.assertEqual(self.policy(valid_until='2027-01-01T00:00:00Z')['time_state'],'in_range')

    def test_bad_dates_and_inverted_intervals_fail_closed(self):
        for fields in ({'valid_until':'nonsense'},{'valid_until':'2026-10-17'},
                       {'valid_until':None},{'valid_from':7},
                       {'valid_from':'2027-01-01T00:00:00Z','valid_until':'2026-01-01T00:00:00Z'},
                       {'valid_from':'2026-01-01T00:00:00Z','valid_until':'2026-01-01T00:00:00Z'}):
            self.assertEqual(self.policy(**fields)['time_state'],'invalid')

    def test_bad_or_foreign_marker_is_not_legacy(self):
        for text in (body(project='Other'),'<!-- memory-project-record {bad} -->'):
            self.assertEqual(self.policy(text=text)['time_state'],'invalid')

    def test_future_as_of_does_not_become_valid_from(self):
        p=self.policy(as_of='2027-01-01T00:00:00Z')
        self.assertEqual(p['time_state'],'invalid')

    def test_no_marker_keeps_legacy_parser_contract(self):
        self.assertIsNone(project_record.parse_record('test note',['example']))
