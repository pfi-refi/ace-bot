// Exercise the real menu controller without making requests or booting Ace.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ace2/app.js'), 'utf8');
const start = source.indexOf('  // A non-modal tools panel:');
const end = source.indexOf("\n  $('clip-btn')", start);
assert(start > 0 && end > start);
let doc;
function element(id, hidden = false) {
  const classes = new Set(hidden ? ['hidden'] : []);
  return {id, listeners: {}, style: {}, attrs: {}, children: [],
    classList: {contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c)},
    addEventListener(k, f) { (this.listeners[k] ||= []).push(f); },
    emit(k, e) { for (const fn of this.listeners[k] || []) fn(e); },
    appendChild(c) { this.children.push(c); },
    contains(c) { return c === this || this.children.some(x => x.contains(c)); },
    setAttribute(k,v) {this.attrs[k]=v;},
    focus() {doc.activeElement=this;},
    querySelector() {return action;},
    getBoundingClientRect() { return this.id==='more-btn' ? {right:144, bottom:400, top:356} : {width:Math.min(420,win.innerWidth-24),height:500}; }
  };
}
const mb=element('more-btn'), drawer=element('quick-more',true), action=element('action'), x=element('more-close'), app=element('app'), outside=element('outside');
drawer.children=[action,x];
const elements={'more-btn':mb,'quick-more':drawer,'more-close':x,'app':app};
doc=element('document'); doc.getElementById=id=>elements[id]; doc.activeElement=mb;
const win=element('window'); win.innerWidth=390;win.innerHeight=844;win.matchMedia=()=>({matches:win.innerWidth>=900});
vm.runInNewContext(source.slice(start,end),{document:doc,window:win});
mb.emit('click',{});
assert.equal(mb.attrs['aria-expanded'],'true'); assert.equal(doc.activeElement,action);
assert.equal(drawer.style.left,'12px'); assert.equal(drawer.style.top,'12px');
let prevented=false;doc.emit('keydown',{key:'Escape',preventDefault(){prevented=true;}});
assert(prevented);assert(drawer.classList.contains('hidden'));assert.equal(doc.activeElement,mb);
mb.emit('click',{});doc.emit('pointerdown',{target:outside});
assert(drawer.classList.contains('hidden'));assert.equal(doc.activeElement,mb);
mb.emit('click',{});doc.activeElement=outside;drawer.emit('click',{target:{closest:()=>action}});
assert(drawer.classList.contains('hidden'));assert.equal(doc.activeElement,outside,'Do not steal focus from an opened surface');
win.innerWidth=1200;mb.emit('click',{});assert.equal(drawer.style.left,'160px');
doc.emit('focusin',{target:outside});assert(drawer.classList.contains('hidden'));
console.log('More menu: open, viewport clamp, Escape, outside close, selection focus and keyboard exit passed.');
