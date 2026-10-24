import os
import hashlib,json,time,subprocess,shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from siri_cli.photon_bridge import PhotonBridge,MailboxService,sign
from siri_cli.relay import Preview,RelayFailure
from test_relay import png
from test_turn_scope import fails
TEST_SIGNING_BYTES=os.urandom(32)

def event(sender='alice',space='space-A',message='m1'):
 now=int(time.time());return dict(project='project',line='fixture-line',sender=sender,space=space,message=message,query='fresh '+sender,created=now,expires=now+120,direction='inbound',platform='imessage')

class Provider:
 def __init__(self):self.calls=[]
 def __call__(self,t):
  self.calls.append(t.incoming);p=t.directory/'new.png';p.write_bytes(png())
  return Preview(t,image=p,scoped=True,fresh_turn=True,identity_verified=True,readable=True)
class Service:
 def __init__(self):self.calls=[];self.receipts={};self.uncertain=False
 def send_png(self,e,run,data):
  self.calls.append(e)
  r=dict(project=e['project'],line=e['line'],space=e['space'],sender=e['sender'],request_message=e['message'],png_sha256=hashlib.sha256(data).hexdigest(),service_message='actual-service-'+run,direction='outbound',platform='imessage')
  self.receipts[run]=r
  if self.uncertain:raise TimeoutError()
  return r
 def receipt_only(self,e,run,digest):return self.receipts.get(run)

def bridge(root,p,s):return PhotonBridge(root,'project','fixture-line',{'space-A':'alice','space-B':'bob'},TEST_SIGNING_BYTES,p,s)

def test_two_external_senders_keep_distinct_original_service_routes():
 with TemporaryDirectory() as d:
  p=Provider();s=Service();b=bridge(d,p,s)
  for e in [event(),event('bob','space-B','m2')]:b.accept(e,sign(e,TEST_SIGNING_BYTES))
  assert b.work_once()['status']=='sent' and b.work_once()['status']=='sent'
  assert [(r['sender'],r['space']) for r in s.calls]==[('alice','space-A'),('bob','space-B')]
  assert [i.account_id for i in p.calls]==['project','project']

def test_duplicate_forged_route_and_stale_job_rejection():
 with TemporaryDirectory() as d:
  b=bridge(d,Provider(),Service());e=event();job=b.accept(e,sign(e,TEST_SIGNING_BYTES));assert b.accept(e,sign(e,TEST_SIGNING_BYTES))==job
  for change in [dict(sender='bob'),dict(space='space-B'),dict(project='other'),dict(line='other'),dict(query='changed'),dict(direction='outbound')]:
   bad={**e,**change};fails(lambda:b.accept(bad,sign(bad,TEST_SIGNING_BYTES)),RelayFailure)
  fails(lambda:b.accept(e,'0'*64),RelayFailure)
  old={**event(message='old'),'created':int(time.time())-200,'expires':int(time.time())-80}
  fails(lambda:b.accept(old,sign(old,TEST_SIGNING_BYTES)),RelayFailure)

def test_service_uncertainty_restart_receipt_only_release_and_no_requery():
 with TemporaryDirectory() as d:
  p=Provider();s=Service();s.uncertain=True;b=bridge(d,p,s)
  for e in [event(),event('bob','space-B','m2')]:b.accept(e,sign(e,TEST_SIGNING_BYTES))
  assert b.work_once()['status']=='delivery_uncertain'
  saved=dict(s.receipts);s.receipts.clear();b=bridge(d,p,s)
  fails(b.work_once,RelayFailure);assert len(p.calls)==len(s.calls)==1
  # Forged receipt cannot release queue.
  run=next(iter(saved));s.receipts[run]={**saved[run],'space':'space-B'}
  fails(b.work_once,RelayFailure);assert len(p.calls)==1
  s.receipts=saved;s.uncertain=False
  assert b.work_once()['status']=='sent' and len(p.calls)==len(s.calls)==2
  assert b.work_once() is None

def test_stale_result_never_enters_service_outbox():
 with TemporaryDirectory() as d:
  p=Provider();s=Service();b=bridge(d,p,s);e=event();b.accept(e,sign(e,TEST_SIGNING_BYTES))
  # Simulate deadline passing during provider; independent epoch gate catches it.
  from unittest.mock import patch
  with patch('siri_cli.photon_bridge.time.time',side_effect=[e['created']+1,e['created']+1,e['expires']+1]):
   assert b.work_once()['status']=='delivery_uncertain'
  assert s.calls==[]

def test_real_js_mailbox_sdk_adapter_and_python_worker_roundtrip_offline():
 node=shutil.which('node');assert node,'Node required for actual JS adapter tests'
 module=(Path('diagnostics/photon/service_port.mjs').resolve()).as_uri()
 with TemporaryDirectory() as d:
  root=Path(d);p=Provider();mail=MailboxService(root/'mail',TEST_SIGNING_BYTES);b=bridge(root/'worker',p,mail)
  e=event();b.accept(e,sign(e,TEST_SIGNING_BYTES));assert b.work_once()['status']=='delivery_uncertain'
  script='''import {servicePort,durableReceipts,drainOne,signedJob} from MODULE;
 const [root,keyHex]=process.argv.slice(1);const key=Buffer.from(keyHex,'hex');
 const receipts=await durableReceipts(root+'/service-receipts');
 const port=servicePort({project:'project',line:'fixture-line',routes:{'space-A':'alice'},live:true,receipts,attachment:(data,opts)=>({data,opts}),resolveOriginalSpace:async id=>({id,type:'dm',send:async builder=>({id:'SDK-OUTBOUND-1',direction:'outbound',platform:'imessage',space:{id},content:{type:'attachment'}})})});
 console.log(JSON.stringify(await drainOne(root,key,port)));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,str(root/'mail'),TEST_SIGNING_BYTES.hex()],capture_output=True,text=True,timeout=15)
  assert r.returncode==0,r.stderr
  assert json.loads(r.stdout)['service_message']=='SDK-OUTBOUND-1'
  b=bridge(root/'worker',p,mail);assert b.work_once() is None and len(p.calls)==1
  assert b.db.execute('SELECT status FROM jobs').fetchone()[0]=='sent_reconciled'


def test_actual_js_signed_event_cross_language_and_nonthrowing_error_receipt():
 node=shutil.which('node');module=(Path('diagnostics/photon/service_port.mjs').resolve()).as_uri()
 with TemporaryDirectory() as d:
  script='''import {signedJob,servicePort,durableReceipts} from MODULE;
 const [root,keyHex]=process.argv.slice(1);const key=Buffer.from(keyHex,'hex');
 const space={id:'space-A',type:'dm',send:async()=>undefined};
 const message={id:'cross-lang',sender:{id:'alice'},platform:'imessage',direction:'inbound',content:{type:'text',text:"what’s weather"}};
 const job=signedJob({project:'project',line:'fixture-line',space,message,key});
 const receipts=await durableReceipts(root+'/receipts');let sends=0;
 space.send=async()=>{sends++;return undefined;};
 const opts={project:'project',line:'fixture-line',routes:{'space-A':'alice'},attachment:(d,o)=>({d,o}),resolveOriginalSpace:async()=>space,receipts,live:true};
 let blocked=0;
 for(let i=0;i<2;i++){const port=servicePort(opts);try{await port.send_png(job.event,'run',Buffer.from('png'));}catch(e){blocked++;}}
 const off=servicePort({...opts,live:false});try{await off.send_png(job.event,'off',Buffer.from('png'));}catch(e){blocked++;}
 console.log(JSON.stringify({job,sends,blocked}));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,d,TEST_SIGNING_BYTES.hex()],capture_output=True,text=True,timeout=15)
  assert r.returncode==0,r.stderr
  value=json.loads(r.stdout);assert value['sends']==1 and value['blocked']==3
  b=bridge(Path(d)/'worker',Provider(),Service());b.accept(value['job']['event'],value['job']['signature'])


def test_text_plus_png_mailbox_keeps_text_and_both_native_receipts():
 node=shutil.which('node');module=Path('diagnostics/photon/service_port.mjs').resolve().as_uri()
 with TemporaryDirectory() as d:
  class TextProvider(Provider):
   def __call__(self,t):
    from dataclasses import replace
    return replace(super().__call__(t),text='It’s cloudy.\n81° 81°')
  p=TextProvider();mail=MailboxService(Path(d)/'mail',TEST_SIGNING_BYTES);b=bridge(Path(d)/'worker',p,mail);e=event();b.accept(e,sign(e,TEST_SIGNING_BYTES));assert b.work_once()['status']=='delivery_uncertain'
  script='''import {servicePort,durableReceipts,drainOne} from MODULE;
 const [root,keyHex]=process.argv.slice(1);let calls=0;
 const receipts=await durableReceipts(root+'/native-receipts');
 const port=servicePort({project:'project',line:'fixture-line',routes:{'space-A':'alice'},live:true,receipts,text:s=>({type:'text',text:s}),attachment:(bytes,opts)=>({type:'attachment',bytes}),resolveOriginalSpace:async id=>({id,type:'dm',send:async(part)=>{calls++;return {id:'native-'+(calls-1),direction:'outbound',platform:'imessage',space:{id},content:part};}})});
 const result=await drainOne(root,Buffer.from(keyHex,'hex'),port);console.log(JSON.stringify({calls,result}));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,str(Path(d)/'mail'),TEST_SIGNING_BYTES.hex()],capture_output=True,text=True,timeout=15);assert r.returncode==0,r.stderr
  out=json.loads(r.stdout);assert out['calls']==2
  assert out['result']['service_messages']==[{'kind':'text','id':'native-0'},{'kind':'attachment','id':'native-1'}]
  b=bridge(Path(d)/'worker',p,mail);assert b.work_once() is None and len(p.calls)==1


def test_multipart_missing_receipt_never_resends():
 node=shutil.which('node');module=Path('diagnostics/photon/service_port.mjs').resolve().as_uri()
 with TemporaryDirectory() as d:
  script='''import {servicePort,durableReceipts} from MODULE;
 const receipts=await durableReceipts(process.argv[1]);let sends=0,blocked=0;
 const e={project:'project',line:'line',space:'space',sender:'alice',message:'incoming',expires:Date.now()/1000+100};
 const port=servicePort({project:'project',line:'line',routes:{space:'alice'},live:true,receipts,text:text=>({text}),attachment:()=>({}),resolveOriginalSpace:async id=>({id,type:'dm',send:async()=>{sends++;return [{id:'text-only',direction:'outbound',platform:'imessage',space:{id},content:{type:'text',text:'answer'}},undefined];}})});
 for(let i=0;i<2;i++)try{await port.send_png(e,'same-run',Buffer.from('png'),'answer');}catch{blocked++;}
 console.log(JSON.stringify({sends,blocked,receipt:await port.receipt_only(e,'same-run','unknown')}));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,d],capture_output=True,text=True,timeout=15);assert r.returncode==0,r.stderr
  assert json.loads(r.stdout)=={'sends':1,'blocked':2,'receipt':None}


def test_separate_send_uncertainty_restart_never_resends_text_or_image():
 node=shutil.which('node');module=Path('diagnostics/photon/service_port.mjs').resolve().as_uri()
 with TemporaryDirectory() as d:
  script='''import {servicePort,durableReceipts} from MODULE;
 const root=process.argv[1];let sends=0,blocked=0;
 const e={project:'project',line:'line',space:'space',sender:'alice',message:'incoming',expires:Date.now()/1000+100};
 const opts={project:'project',line:'line',routes:{space:'alice'},live:true,text:text=>({type:'text',text}),attachment:()=>({type:'attachment'}),resolveOriginalSpace:async id=>({id,type:'dm',send:async content=>{sends++;if(content.type==='attachment')throw Error('timeout after possible delivery');return {id:'text-id',direction:'outbound',platform:'imessage',space:{id},content};}})};
 for(let i=0;i<2;i++){
  const receipts=await durableReceipts(root),port=servicePort({...opts,receipts});
  try{await port.send_png(e,'run',Buffer.from('png'),'answer');}catch{blocked++;}
  if(await port.receipt_only(e,'run','unknown')!==null)throw Error('partial success escaped');
 }
 const parts=await (await durableReceipts(root)).getParts('run');
 console.log(JSON.stringify({sends,blocked,parts:parts.map(p=>p.kind)}));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,d],capture_output=True,text=True,timeout=15);assert r.returncode==0,r.stderr
  assert json.loads(r.stdout)=={'sends':2,'blocked':2,'parts':['text']}


def test_separate_send_restart_reconstructs_only_matching_durable_parts():
 node=shutil.which('node');module=Path('diagnostics/photon/service_port.mjs').resolve().as_uri()
 with TemporaryDirectory() as d:
  script='''import {servicePort,durableReceipts} from MODULE;
 import {createHash} from 'node:crypto';
 import {isDeepStrictEqual} from 'node:util';
 const root=process.argv[1],hash=s=>createHash('sha256').update(s).digest('hex');let sends=0;
 const e={project:'project',line:'line',space:'space',sender:'alice',message:'incoming',expires:Date.now()/1000+100};
 const opts={project:'project',line:'line',routes:{space:'alice'},live:true,text:text=>({type:'text',text}),attachment:()=>({type:'attachment'}),resolveOriginalSpace:async id=>({id,type:'dm',send:async content=>({id:'id-'+(++sends),direction:'outbound',platform:'imessage',space:{id},content})})};
 const durable=await durableReceipts(root);
 try{await servicePort({...opts,receipts:{...durable,record:async()=>{throw Error('crash before aggregate');}}}).send_png(e,'run',Buffer.from('png'),'answer');}catch{}
 const digest=hash(JSON.stringify({png:hash('png'),text:'answer'}));
 const port=servicePort({...opts,receipts:await durableReceipts(root)});
 const recovered=await port.receipt_only(e,'run',digest);
 const again=await port.receipt_only(e,'run',digest);
 let resendBlocked=false;try{await port.send_png(e,'run',Buffer.from('png'),'answer');}catch{resendBlocked=true;}
 const parts=await durable.getParts('run');
 const forged=servicePort({...opts,receipts:{get:async()=>null,getParts:async()=>[parts[0],{...parts[1],sender:'bob'}],record:async()=>{throw Error('forged receipt recorded');}}});
 const inconsistent=servicePort({...opts,receipts:{get:async()=>null,getParts:async()=>[parts[0],{...parts[1],text_sha256:hash('different')}],record:async()=>{throw Error('inconsistent receipt recorded');}}});
 console.log(JSON.stringify({sends,resendBlocked,ids:recovered.service_messages,stable:isDeepStrictEqual(recovered,again),forged:await forged.receipt_only(e,'run',digest),inconsistent:await inconsistent.receipt_only(e,'run',digest)}));
 '''.replace('MODULE',json.dumps(module))
  r=subprocess.run([node,'--input-type=module','-e',script,d],capture_output=True,text=True,timeout=15);assert r.returncode==0,r.stderr
  assert json.loads(r.stdout)=={'sends':2,'resendBlocked':True,'ids':[{'kind':'text','id':'id-1'},{'kind':'attachment','id':'id-2'}],'stable':True,'forged':None,'inconsistent':None}
