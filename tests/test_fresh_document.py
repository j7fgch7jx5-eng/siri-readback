import threading,time
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.document_response import fresh_document_request,DRAWING_QUERY
from siri_cli.preview_provider import Turn
from siri_cli.relay import Ticket,Incoming,RelayFailure

@pytest.mark.parametrize('mode',['success','nonempty','uncertain','wrong_prompt','wrong_bound'])
def test_fresh_binding_precedes_single_submission_and_reaches_document(tmp_path,mode):
 win,pane,c,t,p,r,view,button=[object() for _ in range(8)]
 before=Turn((1,win,pane,c,t,None,None),'',[],0,raw_children=1 if mode=='nonempty' else 0)
 after=Turn((1,win,pane,c,t,p,r),'',['wrong' if mode=='wrong_prompt' else DRAWING_QUERY],1,ready=True)
 calls=[]
 class Port:
  def prepare(self,ticket):calls.append('prepare');return before
  def submit(self,ticket,state):
   assert state.identity[5:]==(None,None);calls.append('submit')
   if mode=='uncertain':raise RuntimeError('uncertain native action')
  def observe(self,ticket):return after
  def pause(self,ticket):raise AssertionError('unexpected repeated observation')
 class UI:
  def bind(self,query):return {'pid':1,'text':'exact\ntext 3','refs':(win,pane,t,c,p,object() if mode=='wrong_bound' else r,view,button)}
 ticket=Ticket('fresh-run',Incoming('owner','local-only',DRAWING_QUERY),tmp_path,time.monotonic()+3,threading.Event())
 from siri_cli.readable_document import ReadableDocument
 artifact=ReadableDocument(tmp_path/'pdf.pdf',1,'hash','com.adobe.pdf',False)
 with patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b),patch('siri_cli.document_response.read_bound_document',return_value=artifact) as read:
  if mode=='success':
   _,result=fresh_document_request(ticket,Port(),UI());assert result['fresh_submission_verified'] and not result['messages_staged'] and not result['sent']
   assert result['text']=='exact\ntext 3' and result['completion']=='unverified';assert read.call_count==1
  else:
   with pytest.raises(RelayFailure):fresh_document_request(ticket,Port(),UI())
   assert read.call_count==0
 assert calls.count('submit')==(0 if mode=='nonempty' else 1)
 assert not (tmp_path/'reply-draft.json').exists()
