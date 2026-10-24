from types import SimpleNamespace
from siri_cli.capture.deadline import bounded_ax_reads
from test_turn_scope import fails


def api():
    calls=[]
    read=lambda *args:calls.append(args) or (0,'value')
    return SimpleNamespace(AXUIElementCopyAttributeValue=read,AXUIElementCopyAttributeNames=read,
        AXUIElementCopyActionNames=read,AXUIElementSetMessagingTimeout=lambda *args:calls.append(args) or 0),calls


def test_ax_read_uses_remaining_time_and_restores_originals():
    a,calls=api();original=a.AXUIElementCopyAttributeValue
    with bounded_ax_reads(12,api=a,clock=lambda:10):a.AXUIElementCopyAttributeValue('ref','AXRole',None)
    assert calls==[('ref',2),('ref','AXRole',None)] and a.AXUIElementCopyAttributeValue is original


def test_expired_deadline_prevents_ax_read():
    a,calls=api()
    with bounded_ax_reads(10,api=a,clock=lambda:11):
        fails(lambda:a.AXUIElementCopyAttributeNames('ref',None),TimeoutError)
    assert not calls


def test_failed_timeout_configuration_prevents_read_and_restores_originals():
    a,calls=api();original=a.AXUIElementCopyActionNames
    a.AXUIElementSetMessagingTimeout=lambda *args:-25204
    with bounded_ax_reads(12,api=a,clock=lambda:10):
        fails(lambda:a.AXUIElementCopyActionNames('ref',None),ValueError)
    assert not calls and a.AXUIElementCopyActionNames is original
