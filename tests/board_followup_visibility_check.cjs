// Run actual view predicates against synthetic rows, without a browser or network.
const fs = require('fs');
const assert = require('assert/strict');
const source = fs.readFileSync(require('path').join(__dirname, '../ace2/app.js'), 'utf8');
const predicates = source.slice(source.indexOf('    function recordOk(x)'), source.indexOf('    // COMPLETION TRUTH'));
const counts = source.slice(source.indexOf('    var nWait='), source.indexOf('    var lenses='));
const cmdCatOf = x => (x.tags || [])[0] || 'Admin';
function view(cmd) {
  return new Function('cmd', 'cmdCatOf', predicates + counts + '\nreturn {items, nWeek, nWait};')(cmd, cmdCatOf);
}
const followup = {id:'follow', entry:'record', status:'open', lane:'waiting', followup:'2026-09-15', bucket:'Personal', tags:['Admin']};
const reference = {id:'ref', entry:'record', status:'open', lane:'ready', bucket:'Personal', tags:['Admin']};
const task = {...followup,id:'task', entry:'task', bucket:'Groundworks'};
const deadline = {...reference,id:'due',due_days:2};
const completed = {...followup,id:'done',status:'done'};
for (const lens of ['today','week','waiting','all']) {
  const cmd = {items:[followup,reference,task,deadline,completed],lens,showRecords:false,showDone:false,area:'All',cat:'All'};
  const result = view(cmd);
  assert(result.items.some(x=>x.id==='follow'), `${lens}: dated record hidden`);
  assert(!result.items.some(x=>x.id==='done'), `${lens}: completed leaked`);
  if(lens!=='waiting') assert(!result.items.some(x=>x.id==='ref'), `${lens}: undated reference leaked`);
  assert.equal(result.nWeek,3);
  assert.equal(result.nWait,2);
  cmd.area='Personal';
  assert.equal(view(cmd).nWeek,2, 'Week count must honor area filter');
  assert.equal(view(cmd).nWait,1, 'Waiting count must honor area filter');
  cmd.cat='Bills';
  assert.equal(view(cmd).items.length,0);
  assert.equal(view(cmd).nWeek,0, 'Week count must honor category filter');
}
console.log('PASS: actual view predicates retain follow-up records, hide reference/completed rows and align filtered counts');
