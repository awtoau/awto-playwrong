// Capture a real Chrome network waterfall of a powderhounds page: shows the long page-document
// bar (~1.2s, uncacheable) vs fast/cached asset bars. The one self-evident "everything's fast
// except the page" artifact. Passes Cloudflare via trusted profile + human mouse. ms-logged.
//
// Output: tmp/waterfall.json (per-resource timings) + tmp/waterfall.html (visual) + tmp/waterfall.png (screenshot)
import { chromium } from "playwright";
import fs from "node:fs";

const URL = process.argv[2] || "https://www.powderhounds.com/Canada/Fernie.aspx";
const TMP = "/home/dan/git/awtoau/powderhounds/tmp";
const OLLAMA = "http://localhost:11434/api/generate";
const rnd = (a,b)=>a+Math.random()*(b-a);
const log = (a,o={}) => { const l=`${new Date().toISOString()} ${a} ${Object.entries(o).map(([k,v])=>k+'='+JSON.stringify(v)).join(' ')}`; fs.appendFileSync(`${TMP}/ph-waterfall.log`,l+"\n"); console.log(l); };

const ctx = await chromium.launchPersistentContext(process.env.HOME+"/ai-browser-profiles/ai-profile",
  { channel:"chrome", headless:false, slowMo:120, viewport:{width:1440,height:900},
    args:["--disable-blink-features=AutomationControlled","--window-size=1440,900"] });
await ctx.addInitScript(()=>Object.defineProperty(navigator,"webdriver",{get:()=>undefined}));
const page = await ctx.newPage();
let lm={x:200,y:200};
async function humanMove(x,y){const s=lm,st=Math.floor(rnd(18,30));const cx=(s.x+x)/2+rnd(-120,120),cy=(s.y+y)/2+rnd(-90,90);for(let i=1;i<=st;i++){const t=i/st;await page.mouse.move((1-t)**2*s.x+2*(1-t)*t*cx+t*t*x+rnd(-1.5,1.5),(1-t)**2*s.y+2*(1-t)*t*cy+t*t*y+rnd(-1.5,1.5));await page.waitForTimeout(rnd(6,22));}await page.mouse.move(x,y);lm={x,y};}
const hasCh=()=>page.frames().some(f=>f.url().includes("challenges.cloudflare.com"));
async function solve(){const b64=(await page.screenshot()).toString("base64");const r=await fetch(OLLAMA,{method:"POST",body:JSON.stringify({model:"qwen2.5vl:7b",prompt:'Cloudflare verify-human checkbox. ONLY {"x":int,"y":int} or {"x":-1,"y":-1}.',images:[b64],stream:false,options:{temperature:0}})});const m=((await r.json()).response||"").match(/\{[^}]*\}/);const loc=m?JSON.parse(m[0]):null;if(loc&&loc.x>0){await humanMove(loc.x,loc.y);await page.waitForTimeout(rnd(120,350));await page.mouse.down();await page.waitForTimeout(rnd(50,120));await page.mouse.up();await page.waitForTimeout(4500);}}

// collect per-resource timing via Performance API after load
const t0 = Date.now();
await page.goto(URL,{waitUntil:"load",timeout:45000}).catch(e=>log("nav",{err:e.message}));
await page.waitForTimeout(2500);
for(let i=0;i<3 && hasCh();i++){ log("challenge",{i}); await solve(); }
await page.waitForTimeout(5000);
log("loaded",{ms:Date.now()-t0});

// pull resource timings from the page
const entries = await page.evaluate(() => {
  const nav = performance.getEntriesByType("navigation")[0] || {};
  const res = performance.getEntriesByType("resource").map(r => ({
    name: r.name, type: r.initiatorType, start: Math.round(r.startTime),
    duration: Math.round(r.duration), size: r.transferSize||0,
  }));
  return { doc: { name: location.href, type: "document", start: 0,
                  duration: Math.round(nav.responseEnd - nav.requestStart),
                  ttfb: Math.round(nav.responseStart - nav.requestStart) }, res };
});
fs.writeFileSync(`${TMP}/waterfall.json`, JSON.stringify(entries,null,2));
await page.screenshot({ path: `${TMP}/waterfall.png`, fullPage:false });

// build a simple SVG waterfall (document bar in red, assets in green)
const all = [{name:"📄 PAGE DOCUMENT (the .aspx)", start:0, duration:entries.doc.duration, doc:true},
  ...entries.res.slice(0,40).map(r=>({name:(r.type+" "+r.name.split("/").pop()).slice(0,40), start:r.start, duration:r.duration, doc:false}))];
const maxT = Math.max(...all.map(a=>a.start+a.duration),1);
const W=900, rowH=16, scale=(W-360)/maxT;
let svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${all.length*rowH+40}" font-family="monospace" font-size="10">`;
svg+=`<text x="6" y="14" font-size="13" font-weight="bold">powderhounds waterfall — page doc (red) vs assets (green). maxT=${maxT}ms</text>`;
all.forEach((a,i)=>{const y=i*rowH+28;const x=360+a.start*scale;const w=Math.max(a.duration*scale,1);
  svg+=`<text x="6" y="${y+10}" fill="${a.doc?'#b00':'#070'}">${a.name}</text>`;
  svg+=`<rect x="${x}" y="${y+2}" width="${w}" height="${rowH-5}" fill="${a.doc?'#e33':'#3a3'}"/>`;
  svg+=`<text x="${x+w+3}" y="${y+10}" fill="#555">${a.duration}ms</text>`;});
svg+="</svg>";
fs.writeFileSync(`${TMP}/waterfall.svg`, svg);
const html=`<!doctype html><body style="font-family:sans-serif"><h2>Powderhounds load waterfall</h2>
<p><b>Page document: ${entries.doc.duration}ms (TTFB ${entries.doc.ttfb}ms)</b> — the red bar. Assets (green) are mostly fast/cached.</p>${svg}
<p>Screenshot:</p><img src="waterfall.png" width="700" style="border:1px solid #ccc"></body>`;
fs.writeFileSync(`${TMP}/waterfall.html`, html);
log("done",{doc_ms:entries.doc.duration, ttfb:entries.doc.ttfb, assets:entries.res.length});
console.log(`\nPAGE DOCUMENT: ${entries.doc.duration}ms (TTFB ${entries.doc.ttfb}ms) vs ${entries.res.length} assets`);
const slowAssets=entries.res.filter(r=>r.duration>200).length;
console.log(`assets slower than 200ms: ${slowAssets}/${entries.res.length}  -> story: page doc dominates`);
await ctx.close();
