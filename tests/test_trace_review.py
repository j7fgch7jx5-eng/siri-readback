import json
from pathlib import Path
from tempfile import TemporaryDirectory
from diagnostics.trace_review import review


def test_disappearing_generation_control_is_reported_without_text():
    rows=[{'kind':'fresh','run_id':'test','elapsed_s':0,'nodes':[
        {'path':[0],'role':'AXButton','identifier':'stop','value':'private content'}]},
        {'kind':'snapshot','run_id':'test','elapsed_s':12,'nodes':[]}]
    with TemporaryDirectory() as temp:
        path=Path(temp)/'trace.jsonl'
        path.write_text('\n'.join(json.dumps(r) for r in rows))
        result=review(path)
    assert result['transitions'][-1]['changes'][0]['change']=='disappeared'
    assert 'private content' not in json.dumps(result)
