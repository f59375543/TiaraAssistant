// Run with Node. The UI gets an isolated document and a simulated loopback service.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

test('initial errors recover independently and a valid JSON edit keeps its cursor', async () => {
  class Element {
    constructor() {
      this.classList = {toggle(){},contains(){return false}};
      this.dataset = {};
      this.style = {};
      this.textContent = '';
      this.selectionStart = 0;
    }
    set value(text){this.text = String(text);this.selectionStart=this.text.length}
    get value(){return this.text||''}
    closest(){return this}
    add(){}
    append(){}
    replaceChildren(){}
    setAttribute(){}
  }
  const fields = new Map();
  const el = id => {
    if(!fields.has(id))fields.set(id,new Element());
    return fields.get(id);
  };
  const document = {getElementById:el,querySelectorAll:()=>[],createElement:()=>new Element(),
    querySelector:el,addEventListener(){}};
  const timers=[];
  const requests={};
  let planVersion='stable',planFailure=false;
  const root=path.join(__dirname,'..');
  const config=JSON.parse(readFileSync(path.join(root,'config.example.json')));
  const fetch=async url=>{
    const route=url.split('?')[0].split('/').pop();
    const n=requests[route]=(requests[route]||0)+1;
    let data;
    let ok=true;
    if(((route==='config'||route==='plan')&&n===1)||(route==='plan'&&planFailure)){ok=false;data={error:'temporary'}}
    else if(route==='status')data={running:false,stream:'test',cursor:0,logs_reset:n===1,
      logs:[],report_version:'1',report:null,plan_version:planVersion};
    else if(route==='config')data=config;
    else if(route==='plan')data={date:'2026-11-26',items:[],version:planVersion};
    else throw Error('Unexpected fetch '+route);
    return {ok,json:async()=>data};
  };
  const sandbox={document,fetch,Option:class{},URLSearchParams,location:{hash:''},
    setTimeout:fn=>{timers.push(fn);return timers.length},clearTimeout(){}};
  vm.runInNewContext(readFileSync(path.join(root,'hka/ui/app.js'),'utf8'),sandbox);
  await new Promise(setImmediate);
  assert.equal(requests.config,1);
  assert.equal(el('home-run').disabled,true);
  await timers.shift()();
  assert.equal(requests.plan,1);
  assert.equal(el('home-run').disabled,true);
  await timers.shift()();
  assert.equal(requests.plan,2);
  assert.equal(el('home-run').disabled,false);
  await timers.shift()();
  assert.equal(requests.plan,2);
  assert.equal(requests.templates,undefined);
  planVersion='updated';
  planFailure=true;
  await timers.shift()();
  assert.equal(el('home-run').disabled,true);
  assert.equal(el('reset-today').disabled,true);
  assert.match(el('plan-date').textContent,/旧计划/);
  planFailure=false;
  await timers.shift()();
  assert.equal(el('home-run').disabled,false);
  assert.equal(el('reset-today').disabled,false);
  const editor=el('task-steps');
  const original='[  { "type": "sleep", "seconds": 1 }  ]';
  editor.value=original;
  editor.selectionStart=4;
  editor.oninput();
  assert.equal(editor.value,original);
  assert.equal(editor.selectionStart,4);
});
