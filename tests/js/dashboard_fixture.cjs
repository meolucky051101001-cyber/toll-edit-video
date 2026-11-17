const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../backend/templates/dashboard.html'), 'utf8');
function slice(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, 'Source boundaries: ' + start);
  return html.slice(a, b);
}
function fixture(extra = {}) {
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
  const elements = new Map();
  function element(id) {
    if (!ids.has(id) && !elements.has(id)) return null;
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {id,style:{},dataset:{},textContent:'',innerHTML:'',value:'',min:'-100',max:'100',checked:false,
        parentElement:{insertAdjacentElement(_position, child){elements.set(child.id, child);}},
        offsetWidth:1,addEventListener(){},querySelectorAll(){return [];},removeAttribute(){},setAttribute(){},pause(){},load(){},getAttribute(){return this.src || '';},
        classList:{add(...xs){xs.forEach(x=>classes.add(x));},remove(...xs){xs.forEach(x=>classes.delete(x));},contains(x){return classes.has(x);}}});
    }
    return elements.get(id);
  }
  const storage = new Map();
  const storageApi = {getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  const context = vm.createContext({console,AbortSignal,AbortController,Headers,URLSearchParams,
    document:{getElementById:element,addEventListener(){},querySelector(){return null;},querySelectorAll(){return [];},
      createElement(){return {style:{},dataset:{}};},
      body:{style:{},classList:{toggle(){}}},hidden:false},
    sessionStorage:storageApi,localStorage:storageApi,location:{hash:''},addEventListener(){},
    MutationObserver:class { observe(){} },
    fetch:async () => { throw new Error('Unexpected request in dashboard fixture'); },
    setTimeout,clearTimeout,setInterval,clearInterval,...extra});
  context.window = context;
  return {context,element,run(code){return vm.runInContext(code,context);}};
}
module.exports={html,slice,fixture};
