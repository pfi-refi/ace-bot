// Actual browser handlers + real API + disposable Postgres. Start ui_server.py on 8831.
const {chromium}=require('/Users/brady/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const base=process.env.ACE_UI_URL||'http://127.0.0.1:8831';
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Local fixture only');
const out=process.env.ACE_UI_OUT||'/tmp/ace-command-center-acceptance';fs.mkdirSync(out,{recursive:true});
(async()=>{const browser=await chromium.launch({headless:true,channel:'chrome'});const results=[];
try{for(const width of [1440,390]){
 const page=await browser.newPage({viewport:{width,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/board-fixture');await page.evaluate(()=>localStorage.setItem('ace2_token','dev'));await page.reload({waitUntil:'networkidle'});
 const read=async()=> (await page.request.get(base+'/daybank?all=true',{headers:{Authorization:'Bearer dev'}})).json();
 for(const old of (await read()).items.filter(x=>/^(Passport renewal appointment|Bicycle wheel alignment|Collect passport supporting|Measure bicycle spoke|Mock permit submission|Mock swimming registration)/.test(x.text))) {
   const r=await page.request.post(base+'/daybank/update',{headers:{Authorization:'Bearer dev'},data:{id:old.id,status:'dropped'}});assert((await r.json()).ok);
 }
 const waitItem=async(test)=>{for(let n=0;n<30;n++){const d=await read();const item=d.items.find(test);if(item)return item;await page.waitForTimeout(100);}throw Error('Persisted item not found');};
 const open=async()=>{await page.click('#command-btn');await page.waitForSelector('.cmd-lens');};
 const area=async name=>width===390?page.locator('#cmd-list-select').selectOption(name):page.locator(`.cmd-area[data-area="${name}"]`).click();
 const row=id=>page.locator(`.cmd-row[data-id="${id}"]`);
 await open();assert(await page.locator(width===390?'#cmd-list-select':'.cmd-areas').isVisible());assert.equal(await page.locator('.cmd-chip').count(),0);
 await area('Personal');const title=(width===1440?'Passport renewal appointment ':'Bicycle wheel alignment ')+Date.now();
 await page.locator('#cmd-input').fill(title);await page.locator('#cmd-input').press('Enter');
 let item=await waitItem(x=>x.text===title);assert.equal(item.bucket,'Personal');assert(item.tags.includes('Personal'));
 await row(item.id).waitFor();await page.locator('#cmd-input').fill(title);await page.locator('#cmd-input').press('Enter');
 await page.waitForTimeout(300);assert.equal((await read()).items.filter(x=>x.text===title).length,1);
 await row(item.id).locator('.cmd-pencil').click();const edited=title+' corrected';
 await page.locator('.cmd-etext').fill(edited);const today=(await read()).today;
 await page.locator('.cmd-edue').fill(today);await page.locator('.cmd-ebucket').selectOption('Groundworks');
 // An actual failed request must retain typed values and show retry state.
 await page.route('**/daybank/update',route=>route.abort());await page.locator('.cmd-esave').click();
 await page.waitForFunction(()=>document.querySelector('.cmd-esave')?.textContent.includes('RETRY'));
 assert.equal(await page.locator('.cmd-etext').inputValue(),edited);assert.equal((await read()).items.find(x=>x.id===item.id).text,title);
 await page.unroute('**/daybank/update');await page.locator('.cmd-esave').click();
 item=await waitItem(x=>x.id===item.id && x.text===edited && x.bucket==='Groundworks' && x.due===today);
 await area('Groundworks');await row(item.id).locator('.cmd-pencil').click();
 page.once('dialog',d=>d.accept(width===1440?'Collect passport supporting documents':'Measure bicycle spoke tension'));
 await page.locator('.cmd-addchild').click();const child=await waitItem(x=>x.parent_id===item.id);assert.equal(child.bucket,'Groundworks');
 await page.locator('.cmd-ecancel:not(.cmd-edrop)').click();
 const blocked=await page.request.post(base+'/daybank/update',{headers:{Authorization:'Bearer dev'},data:{id:item.id,status:'done'}});assert.equal((await blocked.json()).ok,false);
 await row(item.id).locator('.cmd-box').click();await page.locator('.board-confirm-cancel').click();assert.equal((await read()).items.find(x=>x.id===item.id).status,'open');
 await row(child.id).locator('.cmd-box').click();
 await waitItem(x=>x.id===child.id && x.status==='done');assert.equal((await read()).items.find(x=>x.id===item.id).status,'open');
 // Today checkbox and floating Today card share the saved receipt.
 await page.click('#fixture-today');await page.locator('[data-lens="today"]').click();await row(item.id).waitFor();
 await row(item.id).locator('.cmd-box').click();await waitItem(x=>x.id===item.id && x.status==='done');
 await page.waitForTimeout(250);assert.equal(await row(item.id).count(),0);
 const shown=await page.locator('.card[data-panel="DUE TODAY"]').innerText();assert(!shown.includes(edited));
 await page.locator('[data-lens="all"]').click();await page.click('#cmd-filter');await page.click('#cmd-showdone');
 await row(item.id).locator('.cmd-box').click();await waitItem(x=>x.id===item.id&&x.status==='open');
 // A text-derived date can be explicitly cleared, with no title rewrite.
 await area('Personal');let datedTitle=(width===1440?'Mock permit submission':'Mock swimming registration')+' due 25th';
 await page.locator('#cmd-input').fill(datedTitle);await page.locator('#cmd-input').press('Enter');
 let dated=await waitItem(x=>x.text===datedTitle&&x.status==='open');assert(dated.due_on);
 await row(dated.id).locator('.cmd-pencil').click();await page.locator('.cmd-enext').fill('Confirm requirements');await page.locator('.cmd-esave').click();
 dated=await waitItem(x=>x.id===dated.id&&x.next_step==='Confirm requirements');assert.equal(dated.due,null,'unrelated edit must not freeze derived date');
 await row(dated.id).locator('.cmd-pencil').click();await page.locator('.cmd-eclear[data-clears="cmd-edue"]').click();await page.locator('.cmd-esave').click();
 dated=await waitItem(x=>x.id===dated.id&&!x.due_on);assert.equal(dated.text,datedTitle);
 // Scheduled includes dates beyond this week.
 await row(dated.id).locator('.cmd-pencil').click();await page.locator('.cmd-edue').fill('2027-12-20');await page.locator('.cmd-esave').click();
 await waitItem(x=>x.id===dated.id&&x.due_on==='2027-12-20');await page.locator('[data-lens="week"]').click();await row(dated.id).waitFor();
 await area('Groundworks');
 // Records can move between the same life-area lists as actions.
 await area('All');if(!await page.locator('#cmd-recs').isVisible()) await page.click('#cmd-filter');
 if(!((await page.locator('#cmd-recs').getAttribute('class'))||'').includes(' on')) await page.click('#cmd-recs');
 const record=(await read()).items.find(x=>x.text==='The Marlow deal');await row(record.id).locator('.cmd-pencil').click();
 assert(await page.locator('.cmd-ebucket').isEnabled());const target=width===1440?'Personal':'Groundworks';await page.locator('.cmd-ebucket').selectOption(target);await page.locator('.cmd-esave').click();
 await waitItem(x=>x.id===record.id&&x.bucket===target);await area(target);await row(record.id).waitFor();
 const listName='Acceptance list '+width+' '+Date.now();page.once('dialog',d=>d.accept(listName));
 await page.locator(width===390?'#cmd-lists-mobile':'#cmd-lists').click();
 await page.waitForTimeout(400);const lists=await (await page.request.get(base+'/board/lists',{headers:{Authorization:'Bearer dev'}})).json();assert(lists.areas.includes(listName));
 await area('Groundworks');
 // Stale editor must not overwrite a newer external change.
 await row(item.id).locator('.cmd-pencil').click();
 await page.locator('.cmd-etext').fill(edited+' stale local draft');
 let changed=await page.request.post(base+'/daybank/update',{headers:{Authorization:'Bearer dev'},data:{id:item.id,next_step:'Newer update from another device'}});assert((await changed.json()).ok);
 await page.locator('.cmd-esave').click();await page.waitForFunction(()=>document.querySelector('.cmd-esave')?.textContent.includes('RETRY'));
 let fresh=(await read()).items.find(x=>x.id===item.id);assert.equal(fresh.text,edited);assert.equal(fresh.next_step,'Newer update from another device');
 await page.locator('.cmd-ecancel:not(.cmd-edrop)').click();
 // Archive is explicit and reversible from the same UI.
 await row(item.id).locator('.cmd-pencil').click();page.once('dialog',d=>d.dismiss());await page.locator('.cmd-edrop').click();assert.equal((await read()).items.find(x=>x.id===item.id).status,'open');
 page.once('dialog',d=>d.accept());await page.locator('.cmd-edrop').click();await waitItem(x=>x.id===item.id&&x.status==='dropped');
 if(!await page.locator('#cmd-archived').isVisible()) await page.click('#cmd-filter');
 await page.click('#cmd-archived');await row(item.id).locator('.cmd-box').click();await waitItem(x=>x.id===item.id&&x.status==='open');
 // A fresh browser context reads the same data, not an optimistic local copy.
 const other=await browser.newPage({viewport:{width:width===390?1440:390,height:900}});
 await other.goto(base);await other.evaluate(()=>localStorage.setItem('ace2_token','dev'));await other.reload({waitUntil:'networkidle'});await other.click('#command-btn');if(width===1440) await other.locator('#cmd-list-select').selectOption('Groundworks');else await other.locator('.cmd-area[data-area="Groundworks"]').click();
 await other.locator(`.cmd-row[data-id="${item.id}"]`).waitFor();assert((await other.locator(`.cmd-row[data-id="${item.id}"]`).innerText()).includes(edited));await other.close();
 await page.locator('#fixture-today').evaluate(el=>el.style.display='none');await page.locator('#cmd-filter').click();await area('Groundworks');
 await page.screenshot({path:out+'/command-'+width+'.png',fullPage:false});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.deepEqual(errors,[]);
 results.push({width,create:true,duplicatePrevented:true,edit:true,date:true,move:true,failedSavePreservesDraft:true,subtaskPreservesParent:true,unfinishedParentGuard:true,todaySync:true,reopen:true,otherDevice:true,staleSaveRefused:true,archiveCancelRestore:true,derivedDateClear:true,untouchedDuePreserved:true,scheduledBeyondWeek:true,recordMove:true,customList:true,noOverflow:true});await page.close();
}fs.writeFileSync(out+'/results.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results,null,2));}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
