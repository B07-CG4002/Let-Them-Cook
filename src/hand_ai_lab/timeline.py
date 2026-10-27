"""Self-contained local report/annotation editor. No CDN or uploaded sensor data."""
import json
from pathlib import Path

def timeline(path,report,records):
    payload=json.dumps(dict(report=report,records=records),allow_nan=False).replace('<','\\u003c')
    html=r'''<!doctype html><html><meta charset="utf-8"><title>Hand AI replay review</title>
<style>body{font:15px system-ui;margin:24px;max-width:1400px;background:#f5f7fb;color:#182638}h1{font-size:25px}section{background:white;padding:18px;border-radius:12px;margin:14px 0}button,input,select{font:inherit;margin:5px;padding:6px}canvas{width:100%;height:350px;background:#fafbfd}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:6px;border-bottom:1px solid #ddd;text-align:left}.warn{color:#8b3500}pre{white-space:pre-wrap}#hover{min-height:25px}</style>
<h1>Independent-hand AI replay review</h1><p id="scope"></p><p class="warn">Offline replay timing. Scripted demo scores are not model accuracy. Cue blocks are not ground truth.</p>
<section><label>Start <input id="start" type="number" min="0" step=".1" value="0"></label><label>End <input id="end" type="number" min="0" step=".1"></label>
<label>Hand <select id="hand"><option value="0">Both</option><option value="1">LEFT</option><option value="2">RIGHT</option></select></label>
<button id="draw">Update view</button><canvas id="plot"></canvas><div id="hover"></div>
<p>Per hand: truth, raw prediction, then accepted action markers. Gray means unknown/unannotated; color indicates label. Prediction opacity reflects confidence.</p></section>
<section><h2>Reviewed annotations</h2><p>Use observed/video onset and end times. One action interval = one expected acceptance. BACKGROUND is reviewed non-action. UNKNOWN is excluded from scoring.</p>
<label>Hand <select id="d"><option value="1">LEFT</option><option value="2">RIGHT</option></select></label>
<label>Start <input id="a" type="number" min="0" step=".01"></label><label>End <input id="b" type="number" min="0" step=".01"></label><select id="label"></select><button id="add">Add interval</button>
<label><input id="reviewed" type="checkbox">I have reviewed these intervals against observation/video</label><button id="export">Download annotations.json</button><p id="message"></p><table id="segments"></table></section>
<section><h2>Metrics and provenance</h2><pre id="summary"></pre><details><summary>Prediction/event records</summary><table id="records"></table></details></section>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent),r=data.report,rows=data.records;
const labels=['PICKUP','DROP','CHOP','WASH','THROW','POURING','IDLE','BACKGROUND','UNKNOWN'];
const colors=['#137c8b','#dc6843','#456fc9','#3b9b71','#9a58b6','#bd8d19','#718096','#adb5bd','#d7dce3'];
const $=id=>document.getElementById(id);let segments=JSON.parse(JSON.stringify(r.annotations?.segments||[]));
$('scope').textContent=(r.synthetic?'SYNTHETIC TEST — ':'')+r.scope+' | '+r.capture_meta.person+' / '+r.capture_meta.session+' / '+r.capture_meta.split;
$('end').value=r.duration_s.toFixed(2);$('summary').textContent=JSON.stringify({counts:r.counts,health:r.health,settings:r.settings,metrics:r.metrics,models:r.models},null,2);
labels.forEach(x=>{let o=document.createElement('option');o.textContent=x;$('label').append(o)});
function cell(row,text){let x=document.createElement('td');x.textContent=text;row.append(x);return x;}
function showSegments(){let table=$('segments');table.replaceChildren();segments.sort((a,b)=>a.device_id-b.device_id||a.start_s-b.start_s);segments.forEach((s,i)=>{let row=document.createElement('tr');cell(row,s.id);cell(row,s.device_id===1?'LEFT':'RIGHT');cell(row,s.start_s.toFixed(2)+'–'+s.end_s.toFixed(2));cell(row,s.gesture);let b=document.createElement('button');b.textContent='Remove';b.onclick=()=>{segments.splice(i,1);showSegments();draw()};cell(row,'').append(b);table.append(row)});}
function draw(){let c=$('plot'),ctx=c.getContext('2d');c.width=Math.max(c.clientWidth,500)*devicePixelRatio;c.height=350*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);let w=c.width/devicePixelRatio,h=350,lo=+$('start').value,hi=+$('end').value;
 if(!(hi>lo))return;ctx.clearRect(0,0,w,h);let x=t=>90+(t-lo)/(hi-lo)*(w-110),width=(w-110)/(hi-lo),chosen=+$('hand').value;
 for(let d=1;d<=2;d++){if(chosen&&chosen!==d)continue;let y=(d-1)*160+25;ctx.fillStyle='#182638';ctx.fillText(d===1?'LEFT':'RIGHT',8,y+15);['truth','raw','accepted'].forEach((v,i)=>ctx.fillText(v,8,y+i*37+34));
 segments.filter(s=>s.device_id===d&&s.end_s>lo&&s.start_s<hi).forEach(s=>{ctx.fillStyle=colors[labels.indexOf(s.gesture)];ctx.fillRect(x(Math.max(lo,s.start_s)),y+20,(Math.min(hi,s.end_s)-Math.max(lo,s.start_s))*width,22)});
 rows.filter(p=>p.device_id===d&&p.elapsed_s>=lo&&p.elapsed_s<=hi).forEach(p=>{if(p.type==='prediction'){ctx.globalAlpha=Math.max(.15,p.confidence);ctx.fillStyle=colors[labels.indexOf(p.gesture)];ctx.fillRect(x(p.elapsed_s),y+57,Math.max(2,r.settings.interval_s*width),22);ctx.globalAlpha=1}else if(['action','state_start','state_end','idle'].includes(p.type)){ctx.fillStyle=p.type==='state_end'?'#222':colors[labels.indexOf(p.gesture)];ctx.fillRect(x(p.elapsed_s),y+94,3,22);}});}
 ctx.fillStyle='#34465a';for(let i=0;i<=10;i++){let t=lo+(hi-lo)*i/10;ctx.fillText(t.toFixed(1)+'s',x(t)-8,345)}
 let table=$('records');table.replaceChildren();rows.filter(p=>p.elapsed_s>=lo&&p.elapsed_s<=hi&&(!chosen||p.device_id===chosen)).slice(0,500).forEach(p=>{let tr=document.createElement('tr');[p.elapsed_s.toFixed(3),p.device_id,p.type,p.gesture||'',p.confidence?.toFixed(3)||'',p.reason||''].forEach(x=>cell(tr,x));table.append(tr)});
}
$('plot').onmousemove=e=>{let box=e.target.getBoundingClientRect(),t=+$('start').value+(e.clientX-box.left-90)/(box.width-110)*(+$('end').value-+$('start').value);$('hover').textContent='Time '+t.toFixed(3)+' s; use this callback timeline for annotations.';};
$('add').onclick=()=>{let d=+$('d').value,a=+$('a').value,b=+$('b').value;let bounds=r.hand_bounds?.[String(d)]||[0,r.duration_s];if(!(a>=bounds[0]&&b>a&&b<=bounds[1])){alert('Choose a valid interval inside the capture.');return;}if(segments.some(s=>s.device_id===d&&a<s.end_s&&b>s.start_s)){alert('Intervals for one hand cannot overlap.');return;}segments.push({id:'manual-'+Date.now()+'-'+segments.length,device_id:d,start_s:a,end_s:b,gesture:$('label').value});showSegments();draw()};
$('export').onclick=()=>{if(!$('reviewed').checked){alert('Review the intervals, then tick the checkbox.');return;}let a={format:1,capture_sha256:r.capture_sha256,person:r.capture_meta.person,session:r.capture_meta.session,split:r.capture_meta.split,reviewed:true,segments};let url=URL.createObjectURL(new Blob([JSON.stringify(a,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='annotations.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),500);$('message').textContent='Downloaded. Re-run replay with --annotations pointing to that file to compute new metrics.'};
$('draw').onclick=draw;showSegments();draw();window.onresize=draw;
</script></html>'''
    Path(path).write_text(html.replace('PAYLOAD',payload),encoding='utf-8')
