// Thin DSH pre-step adapter. API pattern adapted from zilliztech/memsearch (MIT);
// see THIRD-PARTY-NOTICES.md. No capture, model calls, server or memory store here.
import {execFile} from 'node:child_process';
import {randomUUID} from 'node:crypto';
export const name='lingxi-memory';

export function createPreStep(readContext) {
 return async ({agent,turn,step,signal},next)=>{
  const decision=await next();
  if(decision.kind==='reject'||signal?.aborted||step!==1)return decision;
  const messages=decision.messages||[];
  const user=[...messages].reverse().find(m=>m.role==='user'&&m.source?.kind==='user');
  const prompt=(user?.content||[]).filter(c=>c.type==='text').map(c=>c.text).join('\n');
  if(!prompt)return decision;
  const payload=await readContext({prompt,cwd:agent?.session?.header?.cwd||'',
    session_id:agent?.session?.id||'',turn_id:String(turn)});
  const text=payload?.hookSpecificOutput?.additionalContext;
  if(!text)return decision;
  return {kind:'enter',messages:[...messages,{id:randomUUID(),role:'user',
    content:[{type:'text',text}],source:{kind:'plugin:lingxi-memory',form:'snapshot',
      sections:[{name:'lingxi-background',text}]}}]};
 };
}

export function apply(ctx,options={}) {
 if(!options.python||!options.entry)throw new Error('Lingxi adapter needs installed Python and entry paths');
 const readContext=event=>new Promise(resolve=>{
  const child=execFile(options.python,['-B',options.entry,'hook'],{
    windowsHide:true,timeout:50000,maxBuffer:128*1024,encoding:'utf8',
    env:{...process.env,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'}},(error,stdout)=>{
    if(error){resolve({hookSpecificOutput:{additionalContext:'灵犀背景 service_unavailable：生命周期入口失败；不能宣称接续通过。'}});return;}
    try{resolve(JSON.parse(stdout))}catch{resolve({hookSpecificOutput:{additionalContext:'灵犀背景 service_unavailable：返回格式不支持。'}})}
   });
  child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify(event));
 });
 ctx.on('agent/pre-step',createPreStep(readContext),{prepend:true});
}
