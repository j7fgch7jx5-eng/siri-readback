import sys
from types import SimpleNamespace
from unittest.mock import patch
from siri_cli.driver.siriai import SiriAiBackend
from test_turn_scope import fails


def menu_api(enabled=True, duplicate=False, action_error=0):
    table={'app':{'AXChildren':['bar']}, 'bar':{'AXRole':'AXMenuBar','AXChildren':['file']},
        'file':{'AXTitle':'File','AXChildren':['menu']},
        'menu':{'AXChildren':['new','new'] if duplicate else ['new']},
        'new':{'AXRole':'AXMenuItem','AXTitle':'New Conversation','AXEnabled':enabled}}
    actions=[]
    def press(ref,name):
        actions.append((ref,name))
        return action_error
    api=SimpleNamespace(kAXErrorAttributeUnsupported=-25205,kAXErrorNoValue=-25212,
        AXUIElementCopyAttributeValue=lambda ref,name,_:(0,table[ref].get(name)),
        AXUIElementCopyActionNames=lambda *args:(0,['AXPress']),AXUIElementPerformAction=press)
    return api,actions


def test_windowless_recovery_presses_exact_native_menu_once():
    api,actions=menu_api()
    with patch.dict(sys.modules,ApplicationServices=api):
        assert SiriAiBackend._open_new_conversation_menu('app')
    assert actions==[('new','AXPress')]


def test_disabled_or_ambiguous_menu_never_acts():
    for options in ({'enabled':False},{'duplicate':True}):
        api,actions=menu_api(**options)
        with patch.dict(sys.modules,ApplicationServices=api):
            assert not SiriAiBackend._open_new_conversation_menu('app')
        assert not actions


def test_failed_recovery_action_is_not_retried():
    api,actions=menu_api(action_error=-25204)
    with patch.dict(sys.modules,ApplicationServices=api):
        fails(lambda:SiriAiBackend._open_new_conversation_menu('app'))
    assert len(actions)==1
