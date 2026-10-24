import threading,time
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from siri_cli.relay import Ticket,Incoming,RelayFailure
from siri_cli.document_response import existing_document_request,NativeExistingDocumentUI,DRAWING_QUERY

def test_existing_ui_default_is_disabled():
 with pytest.raises(RelayFailure):NativeExistingDocumentUI()

def test_pipeline_keeps_existing_provenance_and_local_draft_only(tmp_path):
 calls=[];p=SimpleNamespace(count=1);p.change_count=lambda:p.count
 class UI:
  pasteboard=p
  audit=staticmethod(lambda event:None)
  def bind(self,query):calls.append('bind');return {'text':'Exact text 3\nExact text 3 '}
  def open_document(self,b):calls.append('open')
  def open_share(self,b):calls.append('share')
  def verify(self,b):calls.append('verify')
  def copy_document(self,b):calls.append('copy');p.count+=1
 artifact=SimpleNamespace(path=tmp_path/'file.pdf',sha256='fake-hash')
 ticket=Ticket('run',Incoming('owner','fixture-conversation',DRAWING_QUERY),tmp_path,time.monotonic()+5,threading.Event())
 with patch('siri_cli.document_response.extract_readable_after_copy',return_value=artifact):
  a,draft=existing_document_request(ticket,UI())
 assert calls.count('copy')==1 and calls.index('bind')<calls.index('open')<calls.index('share')<calls.index('copy')
 assert draft['text']=='Exact text 3\nExact text 3 ' and draft['sent'] is False
 assert draft['fresh_submission_verified'] is False and draft['completion']=='unverified'
 assert draft['human_review_required'] and (tmp_path/'reply-draft.json').is_file()

def test_wrong_or_replaced_source_prevents_copy_and_draft(tmp_path):
 p=SimpleNamespace(change_count=lambda:1);copies=[]
 class UI:
  pasteboard=p
  audit=staticmethod(lambda e:None)
  def bind(self,q):return {'text':'not approved'}
  def open_document(self,b):pass
  def open_share(self,b):pass
  def verify(self,b):raise RelayFailure('source_changed')
  def copy_document(self,b):copies.append(1)
 t=Ticket('run',Incoming('owner','fixture-conversation',DRAWING_QUERY),tmp_path,time.monotonic()+5,threading.Event())
 with pytest.raises(RelayFailure,match='source_changed'):existing_document_request(t,UI())
 assert copies==[] and not (tmp_path/'reply-draft.json').exists()

@pytest.mark.parametrize('already_active',[False,True])
def test_bounded_foreground_gate_pumps_workspace_notifications(already_active):
 import sys
 import types
 calls=[];workspace=SimpleNamespace(pid=1 if already_active else 2)
 workspace.frontmostApplication=lambda:SimpleNamespace(processIdentifier=lambda:workspace.pid)
 app=SimpleNamespace(activateWithOptions_=lambda options:calls.append('activate') or True)
 k=SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid:app),NSWorkspace=SimpleNamespace(sharedWorkspace=lambda:workspace),NSApplicationActivateIgnoringOtherApps=1)
 def pump(date):calls.append('pump');workspace.pid=1
 f=SimpleNamespace(NSRunLoop=SimpleNamespace(currentRunLoop=lambda:SimpleNamespace(runUntilDate_=pump)),NSDate=SimpleNamespace(dateWithTimeIntervalSinceNow_=lambda value:value))
 ui=NativeExistingDocumentUI.__new__(NativeExistingDocumentUI);ui.deadline=time.monotonic()+1
 ui._source=lambda query:{'pid':1};ui.verify=lambda bound:calls.append('verified')
 with patch.dict(sys.modules,AppKit=k,Foundation=f):assert ui.bind(DRAWING_QUERY)=={'pid':1}
 assert calls==(['verified'] if already_active else ['activate','pump','verified'])
