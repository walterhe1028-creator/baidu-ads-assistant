'use strict';
(() => {
 const DB_NAME='baidu-pages-workspace-v1';
 let apiKey='',model='',analysis,jobs={},chats=[],currentController=null,sequence=0;
 let storageError='';
 const worker=new Worker(new URL('./report-worker.js?v=20261021-evidence',document.currentScript.src));
 const pending=new Map();
 worker.onmessage=({data})=>{const p=pending.get(data.id);if(!p)return;clearTimeout(p.timeout);pending.delete(data.id);data.error?p.reject(Error(data.error)):p.resolve(data.result);};
 worker.onerror=()=>{for(const p of pending.values()){clearTimeout(p.timeout);p.reject(Error('报表引擎加载失败，请检查网络后刷新页面'));}pending.clear();};
 function rpc(op,value){return new Promise((resolve,reject)=>{const id=++sequence,timeout=setTimeout(()=>{pending.delete(id);reject(Error('报表处理超时，请检查网络后刷新重试'));},180000);pending.set(id,{resolve,reject,timeout});worker.postMessage({id,op,value});});}
 function database(){return new Promise((resolve,reject)=>{const req=indexedDB.open(DB_NAME,1);req.onupgradeneeded=()=>req.result.createObjectStore('workspace');req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(Error('浏览器禁止保存本地数据，请关闭无痕模式或允许站点存储'));});}
 const db=database();
 async function stored(){const conn=await db;return new Promise((resolve,reject)=>{const tx=conn.transaction('workspace');const req=tx.objectStore('workspace').get('current');req.onsuccess=()=>resolve(req.result||{});req.onerror=()=>reject(Error('无法读取本机报表'));});}
 async function save(){const snapshot={reports:await rpc('snapshot'),chats:chats.slice(-10),model,latestJob:Object.values(jobs).at(-1)||null};const conn=await db;return new Promise((resolve,reject)=>{const tx=conn.transaction('workspace','readwrite');tx.objectStore('workspace').put(snapshot,'current');tx.oncomplete=resolve;tx.onerror=()=>reject(Error('本机存储空间不足，记录未保存。请下载本轮清单后清理浏览器空间。'));});}
 const ready=(async()=>{const saved=await stored();analysis=await rpc('restore',saved.reports);model=saved.model||'';chats=saved.chats||[];if(saved.latestJob){const j=saved.latestJob;if(j.status==='running'){j.status='error';j.message='页面曾刷新或关闭，模型请求已中断；请重新分析。';j.proposals=[];}jobs[j.id]=j;}})();
 // Make startup failures observable through request(), without an unhandled rejection.
 ready.catch(()=>{});
 function state(){return {...analysis,token:'browser-local',chats,provider:{has_key:!!apiKey,ready:!!apiKey&&!!model,model},active_job:Object.values(jobs).find(j=>j.status==='running')||null,latest_job:Object.values(jobs).at(-1)||null};}
 function json(value,status=200){return new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});}
 async function deepseek(path,payload,controller=new AbortController()){
  if(!apiKey)throw Error('请先输入你自己的DeepSeek密钥');
  const timeout=setTimeout(()=>controller.abort('timeout'),120000);
  try{
   const r=await fetch('https://api.deepseek.com'+path,{method:payload?'POST':'GET',headers:{Authorization:'Bearer '+apiKey,...(payload?{'Content-Type':'application/json'}:{})},body:payload?JSON.stringify(payload):undefined,signal:controller.signal,redirect:'error',credentials:'omit',referrerPolicy:'no-referrer'});
   if(!r.ok){const messages={401:'密钥无效或已失效',402:'DeepSeek余额不足',403:'服务拒绝访问，请检查权限或区域',404:'模型或接口不存在',429:'请求过于频繁或额度不足'};throw Error(messages[r.status]||`DeepSeek暂时无法完成请求（${r.status}）`);}
   const text=await r.text();if(text.length>10*1024*1024)throw Error('模型返回过大，请缩小分析范围');
   try{return JSON.parse(text);}catch{throw Error('DeepSeek未返回可用的数据格式');}
  }catch(e){if(controller.signal.aborted)throw Error(controller.signal.reason==='timeout'?'模型响应超过120秒，本轮已中断，请重试':'已停止本轮分析');if(e instanceof TypeError)throw Error('无法连接DeepSeek，请检查网络；页面没有中转服务器');throw e;}finally{clearTimeout(timeout);}
 }
 async function run(job,question){
  const rows=analysis.recommendations,previous=chats.slice(-3).map(x=>({question:x.question,answer:x.answer.slice(0,5000)}));
  const chunks=[];for(let i=0;i<rows.length;i+=20)chunks.push(rows.slice(i,i+20));if(!chunks.length)chunks.push([]);
  try{
   await save();
   for(let i=0;i<chunks.length;i++){
    if(job.cancelled)break;
    const ids=chunks[i].map(r=>r.id);
    job.message=`正在分析第${i+1}/${chunks.length}批`;
    const payload=await rpc('payload',{revision:job.revision,ids,model,question,conversation:previous});
    if(job.cancelled)break;
    currentController=new AbortController();
    const response=await deepseek('/chat/completions',payload,currentController);
    if(job.cancelled)break;
    const validated=await rpc('validate',{ids,response});
    if(job.cancelled)break;
    job.answer+=(job.answer?'\n\n':'')+`第${i+1}批（${ids.length}项）\n`+validated.answer;
    job.assessments.push(...validated.assessments);job.proposals.push(...validated.proposals);job.done+=ids.length;
    await save();
   }
   if(job.cancelled){job.status='cancelled';job.proposals=[];job.message='已停止分析';}
   else{job.status='complete';job.at=new Date().toLocaleString('sv-SE');chats.push({...job,count:job.total,question,model});chats=chats.slice(-10);}
   await save();
  }catch(e){job.status=job.cancelled?'cancelled':'error';job.message=e.message;job.proposals=[];try{await save();}catch{storageError='结果未能保存到本机，请先下载清单。';}}
  finally{currentController=null;}
 }
 async function request(path,options={}){
  try{
   await ready;if(options.signal?.aborted)throw Error('请求已取消');
   const p=options.body?JSON.parse(options.body):{};
   if(path==='/api/state'){const s=state();if(storageError)s.storageError=storageError;return json(s);}
   if(path==='/api/provider'){
    if(Object.values(jobs).some(j=>j.status==='running'))throw Error('请先停止当前分析');
    if(p.api_key?.trim())apiKey=p.api_key.trim();if(p.model)model=p.model;
    if(apiKey.length>1000||/[\r\n]/.test(apiKey)){apiKey='';throw Error('密钥格式不正确');}
    await save();return json({saved:true});
   }
   if(path==='/api/provider-models'){const r=await deepseek('/models');const models=(r.data||[]).filter(x=>typeof x.id==='string').map(x=>x.id).sort();if(!models.length)throw Error('服务没有返回可用模型');return json({models});}
   if(path==='/api/upload'){
    if(Object.values(jobs).some(j=>j.status==='running'))throw Error('请先停止当前分析');
    if(!Array.isArray(p.files)||!p.files.length||p.files.length>10)throw Error('请选择1—10份CSV报表');
    let size=0;for(const f of p.files){if(typeof f.name!=='string'||!f.name.toLowerCase().endsWith('.csv')||typeof f.content!=='string')throw Error('请上传CSV报表');size+=f.content.length;}
    if(size>60*1024*1024)throw Error('本次上传过大，请分次上传（每次原始文件合计不超过45MB）');
    analysis=await rpc('upload',p);await save();return json({uploaded:p.files.length});
   }
   if(path==='/api/chat'){
    if(!apiKey||!model)throw Error('请先连接DeepSeek');if(!analysis.reports.length)throw Error('请上传报表');
    if(p.revision!==analysis.revision)throw Error('报表已变化，请刷新后重试');
    if(Object.values(jobs).some(j=>j.status==='running'))throw Error('已经有一轮分析正在进行');
    if(typeof p.question!=='string'||!p.question.trim()||p.question.length>10000)throw Error('请用不超过10000字描述目标');
    const job={id:crypto.randomUUID(),status:'running',revision:analysis.revision,total:analysis.recommendations.length,done:0,answer:'',assessments:[],proposals:[],question:p.question};jobs={[job.id]:job};void run(job,p.question);return json(job);
   }
   if(path.startsWith('/api/chat-job/')){const j=jobs[decodeURIComponent(path.slice('/api/chat-job/'.length))];return j?json(j):json({error:'任务已结束或页面已关闭，请重新分析'},404);}
   if(path==='/api/chat-cancel'){const j=jobs[p.id];if(j?.status==='running'){j.cancelled=true;currentController?.abort('cancelled');}return json({cancelled:true});}
   if(path==='/api/export')return new Response(await rpc('export',p),{headers:{'Content-Type':'text/csv;charset=utf-8'}});
   return json({error:'当前浏览器版不支持此操作'},404);
  }catch(e){return json({error:e.message||'操作未完成，请重试'},400);}
 }
 async function clear(){await ready;if(Object.values(jobs).some(j=>j.status==='running'))throw Error('请先停止分析');apiKey='';const conn=await db;await new Promise((resolve,reject)=>{const tx=conn.transaction('workspace','readwrite');tx.objectStore('workspace').clear();tx.oncomplete=resolve;tx.onerror=()=>reject(Error('清空失败，请重试'));});}
 window.addEventListener('pagehide',()=>{apiKey='';currentController?.abort('page closed');});
 window.pagesBackend={request,clear};
})();
