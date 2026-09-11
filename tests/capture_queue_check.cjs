const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('ace2/app.js','utf8');
const start=source.indexOf('  function captureFiles(list) {');
const end=source.indexOf('  // A non-modal tools panel',start);
const pending=[],calls=[];
const ctx={captureBusy:false,captureQueue:[],addAceMessage:()=>{},captureOne:file=>{calls.push(file);return new Promise(resolve=>pending.push(resolve));}};
vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
(async()=>{
ctx.captureFiles(['a','b']);ctx.captureFiles(['c']);
assert.deepEqual(calls,['a']);pending.shift()();await Promise.resolve();
assert.deepEqual(calls,['a','b']);pending.shift()();await Promise.resolve();
assert.deepEqual(calls,['a','b','c']);pending.shift()();await Promise.resolve();
assert.equal(ctx.captureBusy,false);assert.equal(ctx.captureQueue.length,0);
console.log('PASS: selections during upload remain queued, serialized, and drained');
})().catch(e=>{console.error(e);process.exitCode=1;});
