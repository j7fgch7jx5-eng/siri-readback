import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from siri_cli.capture import ax
from diagnostics import drawing_structure_once as diagnostic


@pytest.mark.parametrize('code', [-25202, -25204])
def test_failed_child_without_attributes_keeps_numeric_code_and_path_in_report(tmp_path, code):
    children = {'root': ['left', 'right'], 'right': ['unreadable']}
    reads = []
    def names(raw, unused):
        reads.append(('names', raw))
        return (code, None) if raw == 'unreadable' else (0, ['AXRole'])
    def value(raw, attr, unused):
        reads.append((attr, raw))
        assert raw != 'unreadable', 'failed node must not be queried again'
        if attr == 'AXRole':return 0, 'AXGroup'
        if attr == 'AXChildren':return 0, children.get(raw, [])
        raise AssertionError('unexpected attribute read')
    api = SimpleNamespace(AXUIElementCopyAttributeNames=names,
        AXUIElementCopyAttributeValue=value, AXUIElementCopyActionNames=lambda *a:(0, []),
        kAXErrorAttributeUnsupported=-25205, kAXErrorNoValue=-25212)
    class FailingPreparation:
        def __init__(self, **kwargs):pass
        def prepare(self, ticket):return ax.snapshot_tree('root', structural=True)
        def submit(self, *args):pytest.fail('preparation failure must not submit')
    output = tmp_path/'report'
    with patch.dict(sys.modules, ApplicationServices=api), \
         patch.object(diagnostic, 'StructuralPort', FailingPreparation), \
         patch.object(sys, 'argv', ['probe', '--output', str(output), '--approved-one-query']):
        assert diagnostic.main() == 1
    event = json.loads((output/'structure.jsonl').read_text().splitlines()[-1])
    assert event['ax_failure'] == {'operation':'AXUIElementCopyAttributeNames',
        'error_code':code, 'structural_path':[1, 0], 'path_origin':'snapshot_root',
        'node_attributes_required_for_path':False}
    assert event['retry'] is False
    assert [r for r in reads if r[1]=='unreadable'] == [('names', 'unreadable')]


def test_root_failure_path_is_empty_not_missing():
    error = ax.AXReadError('AXChildren', -25202)
    error.structural_path = ()
    event = diagnostic.failure_event(error)
    assert event['ax_failure']['structural_path'] == []
    assert isinstance(error, ValueError)
