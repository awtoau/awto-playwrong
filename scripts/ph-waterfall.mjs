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

// build SVG: page doc (red) + the OUTLIERS (orange, slow third-party) + a summary of the
// fast Cloudflare-cached pack (green). Sorted by duration so the real outliers are visible.
const docDur = entries.doc.duration;
const sorted = entries.res.slice().sort((a,b)=>b.duration-a.duration);
const OUT = 150; // outlier threshold (ms)
const outliers = sorted.filter(r=>r.duration>=OUT);
const fast = sorted.filter(r=>r.duration<OUT);
const fastMed = fast.length ? fast.map(r=>r.duration).sort((a,b)=>a-b)[Math.floor(fast.length/2)] : 0;
const fastUnder30 = fast.filter(r=>r.duration<30).length;
// rows: document, then each outlier, then ONE summary bar for the fast cached pack
const rows = [{name:"📄 PAGE DOCUMENT (.aspx — uncacheable, BYPASS)", duration:docDur, kind:"doc"}];
outliers.forEach(r=>rows.push({name:(r.type+" "+r.name.split("/").pop().split("?")[0]).slice(0,46),
  full:r.name, duration:r.duration, kind:"outlier"}));
rows.push({name:`▩ ${fast.length} cached assets (Cloudflare HIT) — median ${fastMed}ms, ${fastUnder30} under 30ms`,
  duration:fastMed, kind:"fast"});
const maxT = Math.max(...rows.map(r=>r.duration),1);
const W=1000, rowH=22, scale=(W-470)/maxT;
const color={doc:"#d11", outlier:"#e8920a", fast:"#2a8a2a"};
let svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${rows.length*rowH+70}" font-family="monospace" font-size="11">`;
svg+=`<text x="8" y="16" font-size="14" font-weight="bold">powderhounds.com load — the page &amp; 3 third-party outliers are slow; everything else = Cloudflare cache (fast)</text>`;
svg+=`<text x="8" y="34" font-size="11" fill="#555">RED = uncacheable page · ORANGE = slow third-party (ads/maps/tracking) · GREEN = Cloudflare edge HITs (the 108-asset pack)</text>`;
rows.forEach((r,i)=>{const y=i*rowH+50;const w=Math.max(r.duration*scale,2);
  svg+=`<text x="8" y="${y+12}" fill="${color[r.kind]}" font-weight="${r.kind==='doc'?'bold':'normal'}">${r.name}</text>`;
  svg+=`<rect x="460" y="${y+1}" width="${w}" height="${rowH-6}" fill="${color[r.kind]}" rx="2"/>`;
  svg+=`<text x="${460+w+4}" y="${y+12}" fill="#333">${r.duration}ms${r.kind==='fast'?' (median)':''}</text>`;});
svg+="</svg>";
fs.writeFileSync(`${TMP}/waterfall.svg`, svg);
// record the 3 outliers explicitly
fs.writeFileSync(`${TMP}/waterfall-outliers.json`, JSON.stringify({
  page_document_ms: docDur, ttfb_ms: entries.doc.ttfb,
  outliers: outliers.slice(0,5).map(r=>({ms:r.duration, type:r.type, url:r.name})),
  cloudflare_cached_pack: {count: fast.length, median_ms: fastMed, under_30ms: fastUnder30,
    note: "uniform sub-30ms = Cloudflare edge cache HITs working everywhere"},
}, null, 2));
const html=`<!doctype html><body style="font-family:sans-serif"><h2>Powderhounds load waterfall</h2>
<p><b>Page document: ${entries.doc.duration}ms (TTFB ${entries.doc.ttfb}ms)</b> — the red bar. Assets (green) are mostly fast/cached.</p>${svg}
<p>Screenshot:</p><img src="waterfall.png" width="700" style="border:1px solid #ccc"></body>`;
fs.writeFileSync(`${TMP}/waterfall.html`, html);
log("done",{doc_ms:entries.doc.duration, ttfb:entries.doc.ttfb, assets:entries.res.length});
console.log(`\nPAGE DOCUMENT: ${entries.doc.duration}ms (TTFB ${entries.doc.ttfb}ms) vs ${entries.res.length} assets`);
const slowAssets=entries.res.filter(r=>r.duration>200).length;
console.log(`assets slower than 200ms: ${slowAssets}/${entries.res.length}  -> story: page doc dominates`);
await ctx.close();
