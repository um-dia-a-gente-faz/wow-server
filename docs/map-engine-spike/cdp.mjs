// Minimal Chrome DevTools Protocol driver (Node >= 22, no npm packages, Chrome on PATH as google-chrome).
// Used by bench_run.mjs and proto_check.mjs. Throwaway spike tooling (UM-77).
import { spawn } from "node:child_process";
export async function launch({w=1280,h=800,args=[]}={}) {
  const port = 9300 + Math.floor(Math.random()*500);
  const chrome = spawn("google-chrome", ["--headless=new","--no-sandbox",`--remote-debugging-port=${port}`,`--window-size=${w},${h}`,"--user-data-dir=/tmp/cdp-prof-"+port,"--hide-scrollbars",...args,"about:blank"],{stdio:"ignore"});
  let ws, tries=0, targets;
  while(tries++<50){ try{ targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if(targets.find(t=>t.type==="page")) break;}catch{} await new Promise(r=>setTimeout(r,200)); }
  const page = targets.find(t=>t.type==="page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(r=>ws.onopen=r);
  let id=0; const pend=new Map(); const events=[];
  ws.onmessage = m => { const d=JSON.parse(m.data); if(d.id&&pend.has(d.id)){pend.get(d.id)(d); pend.delete(d.id);} else events.push(d); };
  const send=(method,params={})=>new Promise(res=>{const i=++id; pend.set(i,res); ws.send(JSON.stringify({id:i,method,params}));});
  await send("Page.enable"); await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride",{width:w,height:h,deviceScaleFactor:1,mobile:false});
  const api = {
    send, events,
    async goto(url){ await send("Page.navigate",{url}); await new Promise(r=>setTimeout(r,1500)); },
    async eval(expr){ const r=await send("Runtime.evaluate",{expression:expr,awaitPromise:true,returnByValue:true}); if(r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails)); return r.result.result.value; },
    async shot(path){ const r=await send("Page.captureScreenshot",{format:"png"}); (await import("node:fs")).writeFileSync(path,Buffer.from(r.result.data,"base64")); },
    async click(x,y,button="left"){ for (const type of ["mouseMoved","mousePressed","mouseReleased"]) await send("Input.dispatchMouseEvent",{type,x,y,button,clickCount:1,buttons: type==="mouseReleased"?0:(button==="left"?1:2)}); },
    async move(x,y){ await send("Input.dispatchMouseEvent",{type:"mouseMoved",x,y}); },
    sleep:ms=>new Promise(r=>setTimeout(r,ms)),
    close(){ ws.close(); chrome.kill(); },
  };
  return api;
}
