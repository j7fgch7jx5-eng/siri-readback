// No SDK import, initialization, subscription or network action at module load.
// Inject the EXISTING authenticated project's space and attachment builder.
import {createHmac,createHash} from 'node:crypto';
const hash=data=>createHash('sha256').update(data).digest('hex');
const stable=value=>JSON.stringify(sort(value));
const sort=v=>Array.isArray(v)?v.map(sort):(v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sort(v[k])])):v);
export function signedJob({project,line,space,message,key,now=Math.floor(Date.now()/1000)}) {
 if(message.direction!=='inbound'||message.platform!=='imessage'||message.content?.type!=='text'||space.type!=='dm'||!message.id||!message.sender?.id)throw Error('unsupported inbound event');
 const event={project,line,space:space.id,sender:message.sender.id,message:message.id,query:message.content.text,created:now,expires:now+120,direction:'inbound',platform:'imessage'};
 return {event,signature:createHmac('sha256',key).update(stable(event)).digest('hex')};
}
export function servicePort({project,line,routes,attachment,text,group,resolveOriginalSpace,receipts,live=false}) {
 // receipts is an injected DURABLE service receipt journal, not Mac chat.db.
 // Resolve only an original bound space; never create a chat from a phone.
 const gate=e=>{if(!live)throw Error('live Photon disabled');if(e.project!==project||e.line!==line||routes[e.space]!==e.sender)throw Error('service route mismatch');};
 const snapshot=(e,pngHash,payloadHash,answer,result)=>{
  const results=answer===null?[result]:(result?.content?.type==='group'?result.content.items:result);
  const kinds=answer===null?['attachment']:['text','attachment'];
  if(!Array.isArray(results)||results.length!==kinds.length)throw Error('SDK multipart receipt unverified');
  results.forEach((r,i)=>{if(!r?.id||r.direction!=='outbound'||r.platform!=='imessage'||r.space?.id!==e.space||r.content?.type!==kinds[i])throw Error('SDK send receipt unverified');});
  if(answer!==null&&results[0].content.text!==answer)throw Error('SDK text receipt mismatch');
  return {project,line,space:e.space,sender:e.sender,request_message:e.message,png_sha256:pngHash,payload_sha256:payloadHash,
   ...(answer===null?{}:{text_sha256:hash(answer)}),direction:'outbound',platform:'imessage',service_message:results.at(-1).id,
   service_messages:results.map((r,i)=>({kind:kinds[i],id:r.id}))};
 };
 return {
  async send_png(e,run,png,answer=null) {
   gate(e);if(Date.now()/1000>=e.expires)throw Error('expired job');
   const digest=answer===null?hash(png):hash(stable({png:hash(png),text:answer}));
   // claim must be atomic and durable; an interrupted claim is NEVER resent.
   if(!await receipts.claim(run,e,digest))throw Error('reconcile existing service send');
   const space=await resolveOriginalSpace(e.space);
   if(space.id!==e.space||space.type!=='dm')throw Error('original space replaced');
   const media=attachment(png,{name:`siri-${run}-${digest}.png`,mimeType:'image/png'});
   let result;
   if(answer===null)result=await space.send(media);
   else {
    const sentence=await space.send(text(answer));
    // Validate and durably record text before attempting the image. Do not
    // infer receipt of a multipart child from a single parent message ID.
    if(!sentence?.id||sentence.direction!=='outbound'||sentence.platform!=='imessage'||sentence.space?.id!==e.space||sentence.content?.type!=='text'||sentence.content.text!==answer)throw Error('text receipt unverified');
    await receipts.recordPart(run,'text',{project,line,space:e.space,sender:e.sender,request_message:e.message,payload_sha256:digest,png_sha256:hash(png),text_sha256:hash(answer),direction:'outbound',platform:'imessage',service_message:sentence.id,kind:'text'});
    const picture=await space.send(media);
    if(!picture?.id||picture.direction!=='outbound'||picture.platform!=='imessage'||picture.space?.id!==e.space||picture.content?.type!=='attachment')throw Error('image receipt unverified');
    await receipts.recordPart(run,'attachment',{project,line,space:e.space,sender:e.sender,request_message:e.message,payload_sha256:digest,png_sha256:hash(png),text_sha256:hash(answer),direction:'outbound',platform:'imessage',service_message:picture.id,kind:'attachment'});
    result=[sentence,picture];
   }
   const receipt=snapshot(e,hash(png),digest,answer,result);await receipts.record(run,receipt);return receipt;
  },
  async receipt_only(e,run,digest) {
   gate(e);let receipt=await receipts.get(run);
   if(!receipt&&receipts.getParts){const parts=await receipts.getParts(run);
    if(parts.length===2&&parts[0].kind==='text'&&parts[1].kind==='attachment'&&
       parts[0].service_message!==parts[1].service_message&&
       parts[0].png_sha256===parts[1].png_sha256&&parts[0].text_sha256===parts[1].text_sha256&&
       parts.every(p=>p.project===project&&p.line===line&&p.space===e.space&&p.sender===e.sender&&p.request_message===e.message&&p.payload_sha256===digest&&p.direction==='outbound'&&p.platform==='imessage'&&p.service_message&&/^[a-f0-9]{64}$/.test(p.png_sha256)&&/^[a-f0-9]{64}$/.test(p.text_sha256))){receipt={...parts[1],service_messages:parts.map(p=>({kind:p.kind,id:p.service_message}))};await receipts.record(run,receipt);}
   }
   // No send call or arbitrary history scan. Unknown outbound IDs stay blocked.
   if(!receipt)return null;
   if(receipt.project!==project||receipt.line!==line||receipt.space!==e.space||receipt.sender!==e.sender||receipt.request_message!==e.message||(receipt.payload_sha256??receipt.png_sha256)!==digest||receipt.direction!=='outbound'||receipt.platform!=='imessage'||!receipt.service_message)throw Error('receipt binding mismatch');
   return receipt;
  }
 };
}

import * as fs from 'node:fs/promises';
import {join} from 'node:path';
import {timingSafeEqual,randomUUID} from 'node:crypto';
const filename=run=>hash(run)+'.json';
const signature=(v,key)=>createHmac('sha256',key).update(stable(v)).digest('hex');
async function publish(path,value) {
 const tmp=path+'.'+randomUUID()+'.tmp';const handle=await fs.open(tmp,'wx',0o600);
 try {await handle.writeFile(stable(value));await handle.sync();}finally {await handle.close();}
 try {await fs.link(tmp,path);}finally {await fs.unlink(tmp);}
 const directory=await fs.open(join(path,'..'),'r');try {await directory.sync();}finally {await directory.close();}
}
export async function durableReceipts(root) {
 await fs.mkdir(root,{recursive:true,mode:0o700});
 return {
  async claim(run,event,digest) {
   try {await publish(join(root,filename(run)+'.claim'),{run,event,digest});return true;}
   catch(e){if(e.code==='EEXIST')return false;throw e;}
  },
  async recordPart(run,kind,receipt){await publish(join(root,filename(run)+'.'+kind),receipt);},
  async getParts(run){const parts=[];for(const kind of ['text','attachment'])try{parts.push(JSON.parse(await fs.readFile(join(root,filename(run)+'.'+kind),'utf8')));}catch(e){if(e.code!=='ENOENT')throw e;}return parts;},
  async record(run,receipt) {await publish(join(root,filename(run)),{run,receipt});},
  async get(run) {try {return JSON.parse(await fs.readFile(join(root,filename(run)),'utf8')).receipt;}catch(e){if(e.code==='ENOENT')return null;throw e;}}
 };
}
export async function publishJob(root,job) {
 const directory=join(root,'jobs');await fs.mkdir(directory,{recursive:true,mode:0o700});
 const path=join(directory,hash(job.event.project+'\0'+job.event.message)+'.json');
 try {await publish(path,job);}catch(e){if(e.code!=='EEXIST')throw e;const old=JSON.parse(await fs.readFile(path,'utf8'));if(stable(old)!==stable(job))throw Error('conflicting duplicate event');}
}
export async function drainOne(root,key,port) {
 // Called by the already authorized service process, never a new listener.
 const directory=join(root,'outgoing');await fs.mkdir(directory,{recursive:true,mode:0o700});
 for(const name of (await fs.readdir(directory)).filter(n=>n.endsWith('.json')).sort()) {
  const envelope=JSON.parse(await fs.readFile(join(directory,name),'utf8')),p=envelope.payload;
  const actual=Buffer.from(envelope.signature||'','hex'),expected=Buffer.from(signature(p,key),'hex');
  if(actual.length!==expected.length||!timingSafeEqual(actual,expected))throw Error('forged Mac result');
  if(name!==filename(p.run))throw Error('wrong run path');
  const png=Buffer.from(p.png,'base64');if(hash(png)!==p.png_sha256)throw Error('PNG bytes changed');
  // First reconcile a durable SDK result. Never retry an interrupted claim.
  const payloadHash=p.text===undefined?p.png_sha256:hash(stable({png:p.png_sha256,text:p.text}));
  let receipt=await port.receipt_only(p.event,p.run,payloadHash);
  if(!receipt)receipt=await port.send_png(p.event,p.run,png,p.text??null);
  const payload={run:p.run,receipt};const ack={payload,signature:signature(payload,key)};
  await fs.mkdir(join(root,'receipts'),{recursive:true,mode:0o700});
  try {await publish(join(root,'receipts',name),ack);}catch(e){if(e.code!=='EEXIST')throw e;}
  // Keep original job/result evidence. Skip acknowledged runs on later drains.
  await fs.rename(join(directory,name),join(directory,name+'.acknowledged'));
  return receipt;
 }
 return null;
}
