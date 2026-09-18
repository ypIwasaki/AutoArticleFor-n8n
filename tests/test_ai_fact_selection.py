"""Task scope, reference order, and negative findings in compact packets."""
import copy
import unittest

import ai_input_minimization as inputs


def review_record():
    return dict(
        facts=[
            dict(id='f1', text='First fact', evidenceIds=['e2', 'e1'], topics=['event']),
            dict(id='f2', text='Second fact', evidenceIds=['e1'], topics=['contract']),
            dict(id='f3', text='Third fact', evidenceIds=['e3']),
        ],
        evidence=[
            dict(id='e1', quote='First quote'),
            dict(id='e2', quote='Second quote'),
            dict(id='e3', quote='Third quote'),
        ],
        entities=[
            dict(name='Person', kind='person', factIds=['f2', 'f1']),
            dict(name='Organization', kind='organization', factIds=['f2']),
        ],
    )


class FactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.record = review_record()

    def test_default_scope_depends_on_task(self):
        for task in ('article-summary', 'article-classification'):
            self.assertEqual(inputs.task_facts(self.record, task), self.record['facts'])
        self.assertEqual(inputs.task_facts(self.record, 'talent-index'), self.record['facts'][:2])

    def test_explicit_selection_wins_and_preserves_source_order(self):
        self.record['taskFacts'] = {'talent-index': ['f3', 'f1', 'f3']}
        self.assertEqual(
            inputs.task_facts(self.record, 'talent-index'),
            [self.record['facts'][0], self.record['facts'][2]],
        )

    def test_explicit_empty_scope_does_not_fall_back(self):
        for task in inputs.TASKS:
            self.record['taskFacts'] = {task: []}
            self.assertEqual(inputs.packet(self.record, task), dict(facts=[], evidence=[]))

    def test_no_entity_fact_links_retains_evidence_for_negative_findings(self):
        for entities in ([], [dict(name='Person', factIds=[])]):
            self.record['entities'] = entities
            packet = inputs.packet(self.record, 'talent-index')
            self.assertEqual([fact['id'] for fact in packet['facts']], ['f1', 'f2', 'f3'])
            self.assertEqual(packet['evidence'], self.record['evidence'])
            self.assertNotIn('entities', packet)

    def test_packet_filters_links_without_reordering_or_mutating_source(self):
        self.record['taskFacts'] = {'article-summary': ['f3', 'f1']}
        before = copy.deepcopy(self.record)
        packet = inputs.packet(self.record, 'article-summary')
        self.assertEqual(self.record, before)
        self.assertEqual(packet['facts'], [
            dict(id='f1', text='First fact', evidenceIds=['e2', 'e1']),
            dict(id='f3', text='Third fact', evidenceIds=['e3']),
        ])
        self.assertEqual(packet['evidence'], self.record['evidence'])
        self.assertEqual(packet['entities'], [
            dict(name='Person', kind='person', factIds=['f1']),
        ])

    def test_shared_evidence_is_emitted_once_and_unused_evidence_is_omitted(self):
        packet = inputs.packet(self.record, 'talent-index')
        self.assertEqual([item['id'] for item in packet['evidence']], ['e1', 'e2'])
        self.assertEqual(packet['entities'][0]['factIds'], ['f2', 'f1'])

    def test_other_task_scope_does_not_override_current_task(self):
        self.record['taskFacts'] = {'article-summary': []}
        self.assertEqual(inputs.task_facts(self.record, 'talent-index'), self.record['facts'][:2])

    def test_null_scope_uses_default_and_unknown_ids_are_not_included(self):
        self.record['taskFacts'] = {'article-summary': None}
        self.assertEqual(inputs.task_facts(self.record, 'article-summary'), self.record['facts'])
        self.record['taskFacts'] = {'article-summary': ['missing', 'f2']}
        self.assertEqual(inputs.task_facts(self.record, 'article-summary'), [self.record['facts'][1]])


if __name__ == '__main__':
    unittest.main()
