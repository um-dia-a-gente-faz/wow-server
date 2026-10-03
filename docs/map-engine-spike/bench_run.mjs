// Leaflet vs MapLibre marker benchmark driver for bench.html (UM-77 spike).
//   node bench_run.mjs <gpu|swiftshader> [libs] [counts] [cpuThrottle]
//   e.g. node bench_run.mjs gpu leaflet-dom,maplibre-gl 25,500 6
// BASE = where the build_art.py output dir is served (python3 -m http.server 8765 in that dir).
import { launch } from "./cdp.mjs";
const BASE = process.env.BASE || "http://127.0.0.1:8765";
const mode=process.argv[2]||"gpu";
const gpuArgs = mode==="swiftshader" ? ["--enable-unsafe-swiftshader","--use-angle=swiftshader"] : ["--ignore-gpu-blocklist","--enable-gpu"];
const libs = (process.argv[3]||"leaflet-dom,leaflet-canvas,maplibre-dom,maplibre-gl").split(",");
const ns = (process.argv[4]||"25,100,500,2000").split(",").map(Number);
const metrics = async b => Object.fromEntries((await b.send("Performance.getMetrics")).result.metrics.map(m=>[m.name,m.value]));
for (const lib of libs) for (const n of ns) {
  const b = await launch({w:1280,h:800,args:[...gpuArgs,"--enable-precise-memory-info"]});
  await b.send("Performance.enable"); if (process.argv[5]) await b.send("Emulation.setCPUThrottlingRate",{rate:+process.argv[5]});
  await b.send("Page.navigate",{url:`${BASE}/bench.html?lib=${lib}&n=${n}`});
  let m0=null,m1=null,r=null,t0=0,t1=0;
  for (let i=0;i<1200 && !r;i++){ await b.sleep(20); let ph; try{ ph=await b.eval("window.__phase||''"); }catch{continue}
    if(ph==="anim"&&!m0){ m0=await metrics(b); t0=Date.now(); }
    if(ph==="end"&&!m1){ m1=await metrics(b); t1=Date.now(); }
    if(ph==="end"){ await b.sleep(50); r=JSON.parse(await b.eval("JSON.stringify(window.__result||null)")); } }
  if(!r){ for (const e of b.events) if (e.method==="Runtime.exceptionThrown") console.log(JSON.stringify(e.params).slice(0,400)); console.log("FAIL",lib,n); b.close(); continue; }
  const d=k=>(m1[k]-m0[k]);
  r.mainThreadBusyPct=+(100*d("TaskDuration")/((t1-t0)/1000)).toFixed(0);
  r.scriptPct=+(100*d("ScriptDuration")/((t1-t0)/1000)).toFixed(0);
  r.layoutStylePct=+(100*(d("LayoutDuration")+d("RecalcStyleDuration"))/((t1-t0)/1000)).toFixed(0);
  console.log(JSON.stringify(r)); b.close(); await new Promise(r=>setTimeout(r,300));
}
