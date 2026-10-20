'use strict';
// Runtime is pinned; reports never leave this worker for any remote parser.
let python;
async function init(){
 importScripts('https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide.js');
 python=await loadPyodide({indexURL:'https://cdn.jsdelivr.net/pyodide/v0.27.7/full/'});
 for(const name of ['engine.py','model_protocol.py']){
  const response=await fetch(new URL(name,self.location.href));
  if(!response.ok)throw Error('无法载入报表引擎，请刷新重试');
  python.FS.writeFile('/home/pyodide/'+name,await response.text());
 }
 await python.runPythonAsync(`
import json, base64
from engine import analyze, parse_report, review_rows, csv_bytes, REVIEW_HEADERS
from model_protocol import build_payload, validate_response
reports = {}
def dispatch(op, value):
    global reports
    if op == 'restore':
        reports = value or {}
        return analyze(reports)
    if op == 'upload':
        candidate = dict(reports)
        for f in value['files']:
            raw = base64.b64decode(f['content'], validate=True)
            report = parse_report(f['name'].replace('\\\\','/').split('/')[-1], raw)
            if report['kind'] == 'leads':
                raise ValueError('网页版暂不接收客户跟进明细，请上传百度投放报表；不要上传客户姓名或电话。')
            candidate[report['kind']] = report
        result = analyze(candidate)
        reports = candidate
        return result
    if op == 'snapshot':
        return reports
    if op == 'payload':
        a=analyze(reports)
        if value['revision'] != a['revision']:
            raise ValueError('报表已变化，请重新分析。')
        by_id={r['id']:r for r in a['recommendations']}
        rows=[by_id[rid] for rid in value['ids']]
        context={k:a[k] for k in ('summary','alerts','config','period')}
        context['说明']='本批只包含当前规则候选，不代表全账户所有对象；消费不跨层级相加。'
        return build_payload({'model':value['model']},value['question'],context,rows,value['conversation'])
    if op == 'validate':
        a=analyze(reports)
        by_id={r['id']:r for r in a['recommendations']}
        return validate_response(value['response'],[by_id[rid] for rid in value['ids']])
    if op == 'export':
        a=analyze(reports)
        if value['revision'] != a['revision']:
            raise ValueError('报表已变化，请重新分析后下载。')
        return csv_bytes(REVIEW_HEADERS,review_rows(a,value['selected'],value['edits'])).decode('utf-8-sig')
    raise ValueError('不支持的本机操作')
`);
}
const ready=init();
// Queue RPCs; Python globals must not be shared between concurrent calls.
let queue=Promise.resolve();
self.onmessage=({data})=>{queue=queue.then(async()=>{
 try{
  await ready;
  python.globals.set('rpc_json',JSON.stringify(data));
  const result=await python.runPythonAsync("json.dumps(dispatch(json.loads(rpc_json)['op'], json.loads(rpc_json).get('value')), ensure_ascii=False)");
  self.postMessage({id:data.id,result:JSON.parse(result)});
 }catch(e){const message=String(e.message||e).split('\n').filter(Boolean).at(-1);self.postMessage({id:data.id,error:message});}
});};
