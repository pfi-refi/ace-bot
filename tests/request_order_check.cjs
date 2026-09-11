// Execute the production queue functions in a deterministic transport harness.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const src=fs.readFileSync(require('path').join(__dirname,'../ace2/app.js'),'utf8');
function fn(name){const start=src.indexOf('  function '+name+'('); assert(start>=0); return src.slice(start,src.indexOf('\n  }',start)+4);}
const c={state:{busy:true,micActive:false},activeRequest:'A',pendingSays:[{text:'B'},{text:'C'}],busyWatch:null,ttsPlaying:false,streamMsg:null,sent:[],clearTimeout(){},clearTag(){},setOrbState(){},maybeResumeMic(){},discardEmptyStream(){},collapseTools(){},removeTyping(){},addAceMessage(){},armBusyWatch(){},sendMessage(t){c.activeRequest=t;c.state.busy=true;c.sent.push(t);}};
vm.createContext(c);vm.runInContext(['drainPending','settleTurn','handleWSEvent'].map(fn).join('\n'),c);
c.settleTurn('A');assert.deepEqual(c.sent,['B']);
c.handleWSEvent({type:'done',request_id:'A'});assert.deepEqual(c.sent,['B']);assert(c.state.busy);
c.handleWSEvent({type:'error',request_id:'A',text:'late'});assert.deepEqual(c.sent,['B']);
c.handleWSEvent({type:'done'});assert.deepEqual(c.sent,['B']); // uncorrelated broadcast cannot settle B
c.handleWSEvent({type:'done',request_id:'B'});assert.deepEqual(c.sent,['B','C']);
c.handleWSEvent({type:'done',request_id:'B'});assert.equal(c.activeRequest,'C');assert(c.state.busy);
c.settleTurn('A');assert.equal(c.activeRequest,'C');
c.handleWSEvent({type:'done',request_id:'C'});assert.equal(c.activeRequest,null);assert(!c.state.busy);
console.log('PASS: timeout, late done/error, untagged terminal, duplicate terminal and stale callback isolation');
