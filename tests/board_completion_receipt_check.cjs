// Execute the actual UI completion handler with separate item/Today objects.
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
const source=fs.readFileSync('ace2/app.js','utf8');
const start=source.indexOf("    Array.prototype.forEach.call(v.querySelectorAll('.cmd-box'), function(b)");
const end=source.indexOf('    // FULL EDITING',start);
assert(start>=0&&end>start);
async function run({ok=true,reject=false,guard=false,confirm=true}={}) {
 const original={id:'test',status:'open',text:'Synthetic task',completable:!guard};
 const button={parentNode:{getAttribute:()=>original.id},setAttribute(){},removeAttribute(){}};
 const cmd={items:[original],dueToday:{deadlines:[{...original}]},today:'2026-09-13'};
 const next={items:[{...original,status:'done'}],due_today:{deadlines:[]},today:'2026-09-13',ok,error:ok?undefined:'Refused by server'};
 let posts=0,renders=0,notices=[];
 const ctx={cmd,v:{querySelectorAll:()=>[button]},API:'',headers:()=>({}),confirmBoardCompletion:async()=>confirm,document:{querySelector:()=>null},cmdRender(){renders++},boardNotice(t){notices.push(t)},toLogin(){},fetch:async(url,opts)=>{posts++;assert.equal(JSON.parse(opts.body).force_close,guard?true:undefined);if(reject)throw Error('Network unavailable');return {status:200,json:async()=>next}}};
 vm.runInNewContext(source.slice(start,end),ctx); button.onclick();
 assert.equal(original.status,'open','No unverified optimistic completion');
 await new Promise(r=>setImmediate(r));
 if(!confirm&&guard){assert.equal(posts,0);assert.equal(renders,0);return}
 assert.equal(posts,1);
 if(!ok||reject){assert.equal(cmd.items[0].status,'open');assert.equal(cmd.dueToday.deadlines.length,1);assert.equal(button.disabled,false);assert(notices.length);}
 else {assert.equal(cmd.items[0].status,'done');assert.equal(cmd.dueToday.deadlines.length,0);assert.equal(renders,1);}
}
(async()=>{await run();await run({ok:false});await run({reject:true});await run({guard:true,confirm:false});await run({guard:true});console.log('PASS: receipt refreshes Today and items together; refused/failed saves remain open; waiting close requires confirmation.');})().catch(e=>{console.error(e);process.exit(1)});
