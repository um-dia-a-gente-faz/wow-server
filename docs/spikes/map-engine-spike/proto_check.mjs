// Headless walk-through of prototype.html (UM-77 spike): hit-test, click-to-zoom, crossfade, right-click out,
// satellite toggle and back. Prints the prototype's own state after every step; screenshots go to $OUT
// (default ./shots, keep it OUTSIDE the repo: they contain client art).
//   BASE=http://127.0.0.1:8765 OUT=/tmp/spike-shots node proto_check.mjs
import { launch } from "./cdp.mjs";
import { mkdirSync } from "node:fs";
const BASE = process.env.BASE || "http://127.0.0.1:8765", OUT = process.env.OUT || "./shots";
mkdirSync(OUT, { recursive: true });
const b = await launch({w:1280,h:800});
await b.goto(`${BASE}/prototype.html`);
await b.sleep(2500);
const S=async(l)=>console.log(l, JSON.stringify(await b.eval("__proto.state()")));
await S("start");
// what rects overlap at a point in Durotar and in the Barrens?
for (const [x,y] of [[740,420],[700,440],[600,470]]) {
  await b.move(x,y); await b.sleep(150);
  console.log("hit-test",x,y, await b.eval(`(()=>{const m=__proto.map; const ll=m.containerPointToLatLng([${x},${y}]); return __proto.zonesAt(ll).map(z=>z.name).join(" < ")})()`));
}
await b.move(740,425); await b.sleep(300);
await b.shot(`${OUT}/p2_hover.png`);
await b.click(740,425); await b.sleep(1500);
await S("after click Durotar");
await b.shot(`${OUT}/p3_zone.png`);
await b.eval("__proto.map.setZoom(__proto.map.getZoom()+1.2,{animate:false}); 0"); await b.sleep(900);
await S("deeper");
await b.shot(`${OUT}/p3b_zone_deep.png`);
// mid-fade
await b.eval("__proto.zoomOut(false); 0"); await b.sleep(600);
await b.eval("__proto.zoomToZone('Durotar',false); 0"); await b.sleep(500);
const fit = await b.eval("__proto.map.getZoom()"); 
for (const dz of [-1.2,-0.8,-0.4,0]) { await b.eval(`__proto.map.setZoom(${fit}+(${dz}),{animate:false}); 0`); await b.sleep(600); await S("zoom "+dz); }
await b.click(640,400,"right"); await b.sleep(1500);
await S("after right-click");
await b.eval("__proto.setView('sat'); 0"); await b.sleep(2500);
await S("satellite");
await b.shot(`${OUT}/p4_sat_cont.png`);
await b.eval("__proto.map.setView(__proto.ws(1629,-4373), 6, {animate:false}); 0"); await b.sleep(2500);
await b.shot(`${OUT}/p5_sat_orgrimmar.png`);
await S("sat zoom 6");
await b.eval("__proto.setView('painted'); 0"); await b.sleep(1500);
await S("back to painted");
await b.shot(`${OUT}/p6_back_painted.png`);
b.close();
