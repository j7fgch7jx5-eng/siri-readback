from types import SimpleNamespace
from diagnostics.fullscreen_preparation import ensure_fullscreen, current_fullscreen_window
from test_turn_scope import fails


def test_fullscreen_preparation_waits_for_native_confirmation():
    values=iter([(0,False),(0,False),(0,True)])
    sets=[];pumps=[]
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args:next(values),
        AXUIElementIsAttributeSettable=lambda *args:(0,True),
        AXUIElementSetAttributeValue=lambda *args:sets.append(args) or 0)
    assert ensure_fullscreen(api,'window',lambda:10,pumps.append)=={'fullscreen':True,'changed':True}
    assert sets==[('window','AXFullScreen',True)] and pumps


def test_fullscreen_preparation_does_not_retry_failed_set():
    sets=[]
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args:(0,False),
        AXUIElementIsAttributeSettable=lambda *args:(0,True),
        AXUIElementSetAttributeValue=lambda *args:sets.append(args) or -25204)
    fails(lambda:ensure_fullscreen(api,'window',lambda:10,lambda seconds:None),RuntimeError)
    assert len(sets)==1


def test_fullscreen_read_failure_is_not_false_or_success():
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args:(-25204,None))
    fails(lambda:ensure_fullscreen(api,'window',lambda:10,lambda seconds:None),RuntimeError)


def test_fullscreen_wait_obeys_remaining_deadline():
    remaining_calls=iter([1,1,1,0])
    def remaining():
        if next(remaining_calls)==0:raise TimeoutError('deadline')
        return 1
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args:(0,False),
        AXUIElementIsAttributeSettable=lambda *args:(0,True),
        AXUIElementSetAttributeValue=lambda *args:0)
    fails(lambda:ensure_fullscreen(api,'window',remaining,lambda seconds:None),TimeoutError)


def test_reacquires_window_during_fullscreen_transition_without_setting_again():
    windows=iter([['transition'],['fullscreen']]);pumps=[]
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda ref,*args:(0,ref=='fullscreen'))
    assert current_fullscreen_window(api,lambda:next(windows),lambda:10,pumps.append)=='fullscreen'
    assert pumps==[.1]


def test_reacquired_fullscreen_required_read_failure_aborts():
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args:(-25204,None))
    fails(lambda:current_fullscreen_window(api,lambda:['window'],lambda:10,lambda _:None),RuntimeError)
