import sys,time
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.document_response import NativeExistingDocumentUI
from siri_cli.capture import ax
from siri_cli.relay import RelayFailure


@pytest.mark.parametrize('mode',['ok','cancel','wrong_subrole','uncertain'])
def test_cleanup_only_owned_native_close_button(mode):
 doc,main,button=object(),object(),object();calls=[];verified=[]
 ui=NativeExistingDocumentUI.__new__(NativeExistingDocumentUI);ui.deadline=time.monotonic()+1
 bound={'pid':1,'document':doc};ui.active_bound=bound
 ui.verify=lambda b:verified.append(1)
 def guard():
  if mode=='cancel':raise RelayFailure('cancelled')
 ui.guard=guard;ui.backend=N(_windows=lambda app:[main] if calls else [main,doc])
 def read(ref,attr):
  if ref is doc and attr=='AXCloseButton':return button
  if ref is button and attr=='AXSubrole':return 'AXButton' if mode=='wrong_subrole' else 'AXCloseButton'
  pytest.fail('must not inspect other documents')
 api=N(AXUIElementPerformAction=lambda ref,act:calls.append((ref,act)) or (1 if mode=='uncertain' else 0))
 with patch.dict(sys.modules,ApplicationServices=api),patch.object(ax,'_get_attr_checked',side_effect=read),patch.object(ax,'app_for_pid',return_value=None),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
  if mode=='ok':ui.close_owned_document();assert ui.active_bound is None and 'document' not in bound
  else:
   with pytest.raises(RelayFailure):ui.close_owned_document()
 assert len(calls)==(1 if mode in ('ok','uncertain') else 0)
 if calls:assert calls[0]==(button,'AXPress')
