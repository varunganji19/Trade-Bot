#!/usr/bin/env node
// Fixture-only browser regression: no application imports, database or exchange calls.
// NODE_PATH=/tmp/algo-ui-tools/node_modules node scripts/check_dashboard_ui.cjs
// Optional: --baseline /tmp/algo-dashboard-before.py (screenshot only).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const baseline = process.argv.indexOf('--baseline');
const source = baseline >= 0 ? process.argv[baseline + 1] : path.join(root, 'bot/dashboard.py');
const html = execFileSync('python3', ['-c', `import ast,sys
tree=ast.parse(open(sys.argv[1]).read())
for node in tree.body:
 if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DASHBOARD_HTML' for t in node.targets):
  print(ast.literal_eval(node.value)); break
else: raise RuntimeError('DASHBOARD_HTML not found')`, source], {encoding:'utf8'});
const now = Date.UTC(2026,8,18,12);
const ts = offset => new Date(now + offset * 3600000).toISOString();
const position = {symbol:'BTC/USDT', timeframe:'1h', side:'LONG', qty:0.05, entry:61200,
  mark:62000, stop:60100, target:64500, strategy:'turtle_trend', bars_held:8, unrealized:40, live:true};
const stats = {current_equity:10842.36, start_equity:10000, total_pnl:842.36, return_pct:8.42,
  win_rate:61.7, closed_trades:58, profit_factor:1.72, max_drawdown_pct:2.4,
  open_positions:[position], engine_running:true, cycles:128, watchlist_count:6,
  llm_mode:'quant', trade_modes:{paper:60}, market_mode:'forex', paused:false,
  by_strategy:{turtle_trend:{pnl:620,trades:32,wins:21},vwap_scalper:{pnl:222.36,trades:26,wins:15}}};
const trades = Array.from({length:60}, (_,i) => ({id:i+1, opened_ts:ts(-i*3),
  symbol:i%2?'ETH/USDT':'BTC/USDT', timeframe:i%2?'15m':'1h', side:i%3?'LONG':'SHORT',
  qty:i%2?0.5:0.05, entry_price:i%2?2400:61200, exit_price:i%2?2440:62000,
  pnl:i%4?-12.4:86.3, strategy:i%2?'vwap_scalper':'turtle_trend',
  status:i<2?'OPEN':'CLOSED', exit_reason:i<2?'':i%3?'take_profit':'stop_loss', mode:'paper'}));
const equity = Array.from({length:30},(_,i)=>({ts:ts(i-30),equity:10000+i*27+Math.sin(i)*65}));
const fixtures = {
  '/api/stats':stats, '/api/trades':trades, '/api/equity':equity,
  '/api/decisions':[{ts:ts(-1), action:'HOLD', symbol:'BTC/USDT', timeframe:'1h',
    regime:'trend', confidence:0.78,price:62000,rationale:'Waiting for the next confirmed breakout.'}],
  '/api/watchlist':[], '/api/account/transactions':[], '/api/chat':[], '/api/evidence':{},
  '/api/account':{equity:10842.36,capital:10000,cash:10802.36,unrealized:40,
    engine_running:true,last_equity:{ts:ts(0)}},
  '/api/hft/stats':{...stats,capital:10000,positions:[],total_fees:28.4,engine_running:false},
  '/api/hft/equity':equity,'/api/hft/trades':[],'/api/hft/decisions':[],
  '/api/lab/meta':{timeframes:{standard:{crypto:['1h']},hft:{crypto:['1m']}},
    strategies:{standard:{'1h':['all','turtle_trend']},hft:{'1m':['all']}},
    days_cap:{standard:{crypto:{'1h':365}}},days_default:{standard:{crypto:{'1h':180}}},
    suggestions:{crypto:['BTC/USDT','ETH/USDT']}}, '/api/lab/status':{status:'idle'}
};
let failStats = false;
const mutations = [], unknown = [], errors = [];
const server = http.createServer((req,res) => {
  const url = new URL(req.url,'http://localhost');
  if (req.method !== 'GET') {
    let body=''; req.on('data',chunk=>body+=chunk); req.on('end',()=>{
      mutations.push({method:req.method,path:url.pathname,body:body?JSON.parse(body):null});
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify({status:'closed',exit_price:62000}));
    }); return;
  }
  if (url.pathname === '/') { res.setHeader('Content-Type','text/html'); res.end(html); return; }
  if (['/chart.umd.min.js','/dashboard.css'].includes(url.pathname)) {
    const file = path.join(root,'bot',url.pathname.slice(1));
    if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':'application/javascript');
    res.end(fs.readFileSync(file));return;
  }
  if (url.pathname === '/favicon.ico') {res.writeHead(204);res.end();return;}
  res.setHeader('Content-Type','application/json');
  if (url.pathname === '/api/stats' && failStats) {res.writeHead(503);res.end('{}');return;}
  if (!(url.pathname in fixtures)) {unknown.push(url.pathname);res.writeHead(404);res.end('{}');return;}
  res.end(JSON.stringify(fixtures[url.pathname]));
});
async function main() {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce',colorScheme:'light'});
    // Remote fonts and all other external requests are blocked deliberately.
    await context.route('**/*', route=>route.request().url().startsWith(origin)?route.continue():route.abort());
    const page = await context.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin); await page.locator('#ovStats .stat').first().waitFor();
    if (baseline >= 0) {
      await page.screenshot({path:'/tmp/algo-ui-before.png',fullPage:true});
      console.log('Fixture baseline: /tmp/algo-ui-before.png'); return;
    }
    const screenshot = name=>page.screenshot({path:path.join(root,'docs/screenshots',`dashboard-refresh-${name}.png`),fullPage:true});
    assert.equal(await page.locator('#ovStats .stat').count(),4);
    assert.ok((await page.locator('#overviewSecondary').textContent()).trim());
    assert.ok((await page.locator('#pageDescription').textContent()).trim());
    assert.ok(await page.locator('.paper-badge').isVisible());
    await page.waitForFunction(()=>document.querySelector('#connectionState').textContent.includes('Updated'));
    await screenshot('desktop');
    await page.locator('.theme-switch [data-theme="dark"]').click();
    await page.reload();
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.locator('#ovStats .stat').first().waitFor(); await screenshot('dark');
    await page.locator('.theme-switch [data-theme="light"]').click();
    const navigate = async view=>{
      await page.locator(`#tabs [data-view="${view}"]`).click();
      assert.equal(await page.locator(`#tabs [data-view="${view}"]`).getAttribute('aria-current'),'page');
      assert.ok(await page.locator(`#view-${view}`).isVisible());
    };
    await navigate('portfolio');
    await page.waitForFunction(()=>document.querySelectorAll('#tradeTable tbody tr').length===25);
    assert.match(await page.locator('#tradeCount').textContent(),/1–25 of 60/);
    await page.locator('#tradeNext').click();
    assert.match(await page.locator('#tradeCount').textContent(),/26–50 of 60/);
    await page.locator('#tradeNext').click(); assert.equal(await page.locator('#tradeTable tbody tr').count(),10);
    assert.ok(await page.locator('#tradeNext').isDisabled());
    await page.locator('#tradePrev').click(); assert.equal(await page.locator('#tradeTable tbody tr').count(),25);
    await page.locator('#tradeSearch').fill('BTC');
    assert.match(await page.locator('#tradeCount').textContent(),/1–25 of 30/);
    await page.locator('#tradeStatus').selectOption('CLOSED');
    assert.match(await page.locator('#tradeCount').textContent(),/of 29 matching/);
    await page.locator('#stratFilter').selectOption('vwap_scalper');
    assert.equal(await page.locator('#tradeTable tbody tr').count(),0);
    assert.ok(await page.locator('#tradeEmpty').isVisible());
    await page.locator('#tradeClear').click();
    assert.equal(await page.locator('#tradeTable tbody tr').count(),25);
    await screenshot('portfolio');
    const close = page.locator('#posTable [data-close]').first();
    await close.focus(); await page.keyboard.press('Enter');
    assert.ok(await page.locator('#closeModal').isVisible());
    assert.equal(mutations.length,0,'Opening a confirmation must not mutate');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'closeCancel');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'closeGo');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'closeCancel');
    await page.keyboard.press('Escape');
    assert.ok(!await page.locator('#closeModal').isVisible());
    assert.ok(await close.evaluate(el=>el===document.activeElement));
    await page.keyboard.press('Enter'); await page.locator('#closeGo').click();
    await page.waitForFunction(()=>!document.querySelector('#closeModal').classList.contains('open'));
    assert.deepEqual(mutations,[{method:'POST',path:'/api/positions/close',body:{symbol:'BTC/USDT',timeframe:'1h'}}]);
    for (const view of ['hft','watchlist','lab','evidence','account','chat']) await navigate(view);
    stats.engine_running=false;
    await page.waitForFunction(()=>document.querySelector('#enginePillText').textContent.toLowerCase().includes('stopped'),{},{timeout:6500});
    failStats=true;
    await page.waitForFunction(()=>document.querySelector('#connectionState').textContent.includes('stale'),{},{timeout:6500});
    assert.ok(await page.locator('#connectionBanner').isVisible());
    failStats=false;stats.engine_running=true;
    await navigate('overview');
    await page.waitForFunction(()=>document.querySelector('#connectionState').textContent.includes('Updated'));
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
    const geometry = await page.evaluate(()=>({width:innerWidth,body:document.body.scrollWidth,
      header:document.querySelector('header').getBoundingClientRect().bottom,
      tabs:document.querySelector('#tabs').getBoundingClientRect().top}));
    assert.ok(geometry.body<=geometry.width,JSON.stringify(geometry));
    assert.ok(geometry.tabs>=geometry.header-1,JSON.stringify(geometry));
    await screenshot('mobile');
    for (const view of ['portfolio','hft','watchlist','lab','evidence','account','chat']) {
      await navigate(view);
      const fits = await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
      if (!fits) {
        await page.screenshot({path:`/tmp/algo-ui-overflow-${view}.png`,fullPage:true});
        console.error(await page.evaluate(()=>Array.from(document.querySelectorAll('.view.active *'))
          .filter(el=>el.getBoundingClientRect().right>innerWidth)
          .slice(0,12).map(el=>({tag:el.tagName,id:el.id,cls:el.className,right:el.getBoundingClientRect().right}))));
      }
      assert.ok(fits,
        `No horizontal page overflow on mobile ${view}`);
    }
    assert.deepEqual(unknown,[],'Every requested API must have an explicit fixture');
    assert.deepEqual(errors,[],'No uncaught browser errors');
    console.log('PASS: overview, navigation, themes, filters, pagination, confirmed close, keyboard focus, background status, stale state, mobile layout. Screenshots use synthetic fixture data only.');
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>server.close());
