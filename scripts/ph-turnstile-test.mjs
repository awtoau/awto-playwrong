// Turnstile test harness: open ONE browser, try to pass the challenge with human-like vision-click.
// If it FAILS, delete the Cloudflare cookies (cf_clearance / cf_chl_* — the bot-flag) and retry in
// the SAME session. Lets us iterate on passing Turnstile without relaunching each time.
import { chromium } from "playwright";

const URL = process.argv[2] || "https://www.powderhounds.com/Canada/Fernie.aspx";
const OLLAMA = "http://localhost:11434/api/generate";
const MODEL = process.env.PH_VLM || "qwen2.5vl:7b";
const MAX_ROUNDS = 4;

const rnd = (a,b)=>a+Math.random()*(b-a);

async function humanMove(page, x, y) {
  const s = page._lm || { x: rnd(100,400), y: rnd(100,300) };
  const steps = Math.floor(rnd(18,30));
  const cx=(s.x+x)/2+rnd(-120,120), cy=(s.y+y)/2+rnd(-90,90);
  for (let i=1;i<=steps;i++){const t=i/steps;
    const bx=(1-t)**2*s.x+2*(1-t)*t*cx+t*t*x, by=(1-t)**2*s.y+2*(1-t)*t*cy+t*t*y;
    await page.mouse.move(bx+rnd(-1.5,1.5),by+rnd(-1.5,1.5)); await page.waitForTimeout(rnd(6,22));}
  await page.mouse.move(x+rnd(-4,4),y+rnd(-4,4)); await page.waitForTimeout(rnd(40,120));
  await page.mouse.move(x,y); page._lm={x,y};
}
const hasChallenge = (page) => page.frames().some(f=>f.url().includes("challenges.cloudflare.com"));
async function passed(page){
  if(hasChallenge(page))return false;
  const t=await page.title().catch(()=>"");
  return !/just a moment|attention required|verify you are human/i.test(t);
}
async function visionClick(page){
  const vp=page.viewportSize()||{width:1440,height:810};
  const b64=(await page.screenshot()).toString("base64");
  const prompt=`This ${vp.width}x${vp.height} screenshot shows a Cloudflare "Verify you are human" `+
    `checkbox (small square on the left of a wide grey/blue widget mid-page). Return ONLY `+
    `{"x":int,"y":int} = checkbox center, or {"x":-1,"y":-1}.`;
  const r=await fetch(OLLAMA,{method:"POST",body:JSON.stringify({model:MODEL,prompt,images:[b64],stream:false,options:{temperature:0}})});
  const m=((await r.json()).response||"").match(/\{[^}]*\}/); const loc=m?JSON.parse(m[0]):null;
  if(loc&&loc.x>0){await humanMove(page,loc.x,loc.y);await page.waitForTimeout(rnd(120,350));
    await page.mouse.down();await page.waitForTimeout(rnd(50,120));await page.mouse.up();
    console.log(`    clicked (${loc.x},${loc.y})`);await page.waitForTimeout(4500);return true;}
  console.log("    VLM saw no checkbox");return false;
}
async function clearCfCookies(ctx){
  const all = await ctx.cookies();
  const keep = all.filter(c=>!/cf_clearance|cf_chl|__cf_bm/i.test(c.name) && !/cloudflare/i.test(c.domain));
  await ctx.clearCookies();
  await ctx.addCookies(keep);  // restore non-cloudflare cookies (keep GA, cart, logins)
  console.log(`    cleared ${all.length-keep.length} cloudflare cookie(s), kept ${keep.length}`);
}

const ctx = await chromium.launchPersistentContext(
  process.env.HOME + "/ai-browser-profiles/ai-profile",
  { channel:"chrome", headless:false, slowMo:120, viewport:{width:1440,height:810},
    args:["--disable-blink-features=AutomationControlled","--window-size=1440,810","--window-position=120,80"] }
);
await ctx.addInitScript(()=>{Object.defineProperty(navigator,"webdriver",{get:()=>undefined});});
const page = await ctx.newPage();

for (let round=1; round<=MAX_ROUNDS; round++){
  console.log(`\n=== round ${round} ===`);
  await page.goto(URL,{waitUntil:"load",timeout:45000}).catch(e=>console.log("  nav:",e.message));
  await page.waitForTimeout(3000);
  // try to solve up to 2 vision clicks this round
  for(let a=0;a<2 && hasChallenge(page);a++) await visionClick(page);
  await page.waitForTimeout(3000);
  if(await passed(page)){
    console.log(`\n✅ PASSED on round ${round}. Real page loaded.`);
    console.log("   title:", (await page.title()).slice(0,60));
    break;
  }
  console.log(`  ❌ round ${round} failed — clearing cloudflare cookies and retrying`);
  await clearCfCookies(ctx);
  if(round===MAX_ROUNDS) console.log("\n❌ all rounds failed.");
}
await page.waitForTimeout(2000);
await ctx.close();
