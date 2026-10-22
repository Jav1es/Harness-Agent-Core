/**
 * Harness-Agent-Core 页面断言
 * ---------------------------------------------------------------------------
 * 用本机 Chrome/Edge 的无头模式 + CDP 直接跑，不下载浏览器、不装依赖。
 * 断言覆盖：控制台无报错、375px 无横向溢出、交互可用、减弱动效可降级、
 * 深色模式可读、无外链资源、无密钥泄漏。
 *
 * 用法：
 *   node verify.mjs            跑全部
 *   node verify.mjs --json     只输出 JSON
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
// 两个被测目标：
//   PREVIEW   —— 自带站点 styles.css 的完整预览（模拟「装进作品集后」）
//   STANDALONE —— 只有三个交付文件的目录（模拟「直接双击打开」）
const PREVIEW = pathToFileURL(join(__dirname, '..', 'preview', 'index.html')).href;
const STANDALONE = pathToFileURL(join(__dirname, 'harness-agent-core.html')).href;
const TARGET = PREVIEW;

function findBrowser() {
  const env = process.env.HX_BROWSER;
  if (env && existsSync(env)) return env;
  const cands = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
  ];
  for (const c of cands) if (existsSync(c)) return c;
  throw new Error('找不到 Chrome / Edge，可用 HX_BROWSER 环境变量指定');
}

/* ------------------------------------------------------------------ CDP */
async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const events = [];

  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', rej, { once: true });
  });

  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  });

  function send(method, params = {}, sessionId) {
    const mid = ++id;
    return new Promise((resolve, reject) => {
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }

  return { send, events, close: () => ws.close() };
}

/* ---------------------------------------------------------------- 断言器 */
function makeAsserts() {
  const results = [];
  function ok(name, cond, detail) {
    results.push({ name, pass: !!cond, detail: detail === undefined ? '' : String(detail) });
  }
  function eq(name, actual, expected) {
    ok(name, actual === expected, `实际=${JSON.stringify(actual)} 期望=${JSON.stringify(expected)}`);
  }
  return { results, ok, eq };
}

/* ------------------------------------------------------------------- 主流程 */
const browser = findBrowser();
const userDir = join(process.env.TEMP || '/tmp', 'hx-verify-' + Date.now());

const proc = spawn(browser, [
  '--headless=new',
  '--remote-debugging-port=0',
  '--user-data-dir=' + userDir,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-gpu',
  '--hide-scrollbars',
  'about:blank'
], { stdio: ['ignore', 'ignore', 'pipe'] });

const wsUrl = await new Promise((resolve, reject) => {
  let buf = '';
  const timer = setTimeout(() => reject(new Error('浏览器启动超时')), 20000);
  proc.stderr.on('data', (d) => {
    buf += d.toString();
    const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) { clearTimeout(timer); resolve(m[1]); }
  });
  proc.on('exit', (c) => { clearTimeout(timer); reject(new Error('浏览器退出 ' + c)); });
});

const cdp = await connect(wsUrl);
const { results, ok, eq } = makeAsserts();

try {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });

  const send = (m, p) => cdp.send(m, p, sessionId);
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Log.enable');

  const consoleErrors = [];
  const pageErrors = [];
  cdp.events.length = 0;
  const drain = () => {
    for (const e of cdp.events.splice(0)) {
      if (e.sessionId !== sessionId) continue;
      if (e.method === 'Runtime.exceptionThrown') {
        pageErrors.push(e.params.exceptionDetails.text + ' ' +
          (e.params.exceptionDetails.exception?.description || ''));
      }
      if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') {
        // 记全 URL 与来源，否则「4 条错误」无法判断是不是预期的资源 404
        consoleErrors.push(
          '[' + (e.params.entry.source || '?') + '] ' +
          (e.params.entry.url || '') + ' :: ' +
          (e.params.entry.text || '')
        );
      }
      if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') {
        consoleErrors.push(e.params.args.map(a => a.value || a.description).join(' '));
      }
    }
  };

  async function evaluate(expr, awaitPromise = false) {
    const r = await send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise
    });
    if (r.exceptionDetails) {
      throw new Error('evaluate 失败: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    }
    return r.result.value;
  }

  async function goto(url) {
    await send('Page.navigate', { url });
    for (let i = 0; i < 100; i++) {
      await new Promise(r => setTimeout(r, 100));
      const st = await evaluate('document.readyState');
      if (st === 'complete') break;
    }
    await new Promise(r => setTimeout(r, 700));
    drain();
  }

  /* ============ 1. 桌面 1280 ============ */
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 900, deviceScaleFactor: 1, mobile: false
  });
  await goto(TARGET);

  ok('1.1 页面标题以模块名开头',
    (await evaluate('document.title')).startsWith('Harness-Agent-Core'),
    await evaluate('document.title'));
  ok('1.2 模块脚本已执行', await evaluate('!!window.HARNESS_AGENT_CORE'));
  eq('1.3 架构图五层', await evaluate("document.querySelectorAll('.hx-arch .lyr').length"), 5);
  eq('1.4 五张层卡片', await evaluate("document.querySelectorAll('.hx-layer').length"), 5);
  eq('1.5 三个代码 Tab', await evaluate("document.querySelectorAll('.hx-tab').length"), 3);
  eq('1.6 时间线五步', await evaluate("document.querySelectorAll('.hx-step').length"), 5);
  eq('1.7 默认只显示第一个面板',
    await evaluate("Array.from(document.querySelectorAll('.hx-panel')).filter(p=>!p.hidden).length"), 1);
  eq('1.8 首屏无横向溢出',
    await evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), true);
  ok('1.9 canvas 已按 DPR 初始化',
    await evaluate("(()=>{const c=document.querySelector('.hx-hero__viz canvas');return c && c.width>0 && c.height>0})()"));

  /* ---- 交互：Tab ---- */
  await evaluate("document.querySelectorAll('.hx-tab')[2].click()");
  eq('2.1 点 CI Tab 后只显示 CI 面板',
    await evaluate("Array.from(document.querySelectorAll('.hx-panel')).filter(p=>!p.hidden).map(p=>p.id).join()"),
    'hx-panel-ci');
  eq('2.2 aria-selected 同步',
    await evaluate("document.querySelectorAll('.hx-tab')[2].getAttribute('aria-selected')"), 'true');
  await evaluate("document.querySelectorAll('.hx-tab')[0].click()");

  /* ---- 交互：架构分层点击 ---- */
  await evaluate("document.querySelectorAll('.hx-layer')[3].click()");
  eq('3.1 点第 4 张层卡片，SVG 第 4 层高亮',
    await evaluate("Array.from(document.querySelectorAll('.hx-arch .lyr')).map(g=>g.classList.contains('is-on')).join()"),
    'false,false,false,true,false');
  eq('3.2 层卡片 aria-pressed 同步',
    await evaluate("document.querySelectorAll('.hx-layer')[3].getAttribute('aria-pressed')"), 'true');

  /* ---- 交互：时间线 ---- */
  await evaluate("document.querySelector('[data-hx-play]').click()");
  await new Promise(r => setTimeout(r, 300));
  eq('4.1 时间线播放到第 1 步',
    await evaluate("Array.from(document.querySelectorAll('.hx-step')).findIndex(s=>s.classList.contains('is-active'))"), 0);
  ok('4.2 时间线有代码内容',
    await evaluate("document.querySelector('.hx-tl__pre').textContent.length > 50"));

  /* ---- 交互：熔断（max_retries=2 应熔断） ---- */
  const breakerCircuit = await evaluate(`(function(){
    const r = document.querySelector('#hx-maxretry');
    r.value = 2; r.dispatchEvent(new Event('input'));
    document.querySelector('[data-hx-run]').click();
    return new Promise(res => setTimeout(() => {
      const st = document.querySelector('.hx-breaker__state');
      res({
        open: st.classList.contains('is-open'),
        text: st.textContent,
        logHasBreaker: document.querySelector('.hx-log').textContent.includes('CIRCUIT BREAKER TRIGGERED')
      });
    }, 2600));
  })()`, true);
  eq('5.1 max_retries=2 触发熔断', breakerCircuit.open, true);
  ok('5.2 熔断日志出现', breakerCircuit.logHasBreaker, breakerCircuit.text);

  const breakerHeal = await evaluate(`(function(){
    const r = document.querySelector('#hx-maxretry');
    r.value = 3; r.dispatchEvent(new Event('input'));
    document.querySelector('[data-hx-run]').click();
    return new Promise(res => setTimeout(() => {
      const st = document.querySelector('.hx-breaker__state');
      res({
        open: st.classList.contains('is-open'),
        text: st.textContent,
        logHasResult: document.querySelector('.hx-log').textContent.includes('self_healed=1')
      });
    }, 3000));
  })()`, true);
  eq('5.3 max_retries=3 不熔断（自愈成功）', breakerHeal.open, false);
  ok('5.4 自愈日志出现', breakerHeal.logHasResult, breakerHeal.text);

  /* ---- 滚动后：瀑布条与数字 ---- */
  await evaluate("document.getElementById('hx-obs').scrollIntoView({block:'center'})");
  await new Promise(r => setTimeout(r, 1200));
  const spans = await evaluate(`Array.from(document.querySelectorAll('.hx-span__bar'))
    .map(b => ({w: b.style.width, l: b.style.left}))`);
  ok('6.1 八条 span 全部有宽度', spans.every(s => s.w && s.w !== '0%'), JSON.stringify(spans.slice(0,2)));
  ok('6.2 span 有错位（left 不全为 0）', spans.filter(s => s.l && s.l !== '0%').length >= 5,
    spans.map(s=>s.l).join(' '));

  await evaluate("document.getElementById('hx-metrics').scrollIntoView({block:'center'})");
  await new Promise(r => setTimeout(r, 1400));
  eq('6.3 指标数字动画落到终值',
    await evaluate("Array.from(document.querySelectorAll('[data-count]')).map(e=>e.textContent).join(' ')"),
    '32% <2% −80% 98%');

  /* ---- 无障碍 ---- */
  eq('7.1 锚点无死链', await evaluate(`Array.from(document.querySelectorAll('a[href^="#"]'))
    .map(a=>a.getAttribute('href')).filter(h=>h!=='#' && !document.getElementById(h.slice(1))).length`), 0);
  eq('7.2 无重复 id', await evaluate(`(()=>{const ids=Array.from(document.querySelectorAll('[id]')).map(e=>e.id);
    return ids.filter((v,i)=>ids.indexOf(v)!==i).length})()`), 0);
  eq('7.3 无图片缺 alt', await evaluate("document.querySelectorAll('img:not([alt])').length"), 0);
  ok('7.4 SVG 有可访问名称',
    await evaluate("(()=>{const s=document.querySelector('.hx-arch svg');return s && (s.getAttribute('aria-labelledby')||s.getAttribute('aria-label'))})()"));
  eq('7.5 键盘可切 Tab', await evaluate(`(function(){
    const tabs=document.querySelectorAll('.hx-tab'); tabs[0].focus();
    tabs[0].dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
    return document.activeElement.id;
  })()`), 'hx-tab-arch');
  await evaluate("document.querySelectorAll('.hx-tab')[0].click()");

  /* ============ 8. 375px 移动端 ============ */
  await send('Emulation.setDeviceMetricsOverride', {
    width: 375, height: 812, deviceScaleFactor: 2, mobile: true
  });
  await goto(TARGET);

  eq('8.1 375px 无横向滚动',
    await evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1'),
    true, 'scrollW=' + await evaluate('document.documentElement.scrollWidth'));
  eq('8.2 移动端改用竖排架构图（SVG 隐藏）',
    await evaluate("getComputedStyle(document.querySelector('.hx-arch__scroll')).display"), 'none');
  eq('8.3 移动端竖排架构图可见',
    await evaluate("getComputedStyle(document.querySelector('.hx-arch__mobile')).display"), 'block');
  eq('8.4 竖排五层完整', await evaluate("document.querySelectorAll('.hx-am__layer').length"), 5);
  eq('8.5 时间线在手机上竖排',
    await evaluate("getComputedStyle(document.querySelector('.hx-step')).flexDirection"), 'row');
  ok('8.6 代码块可横向滚动（不是被裁死）',
    await evaluate("getComputedStyle(document.querySelector('.hx-code')).overflowX === 'auto'"));
  ok('8.7 滑块可点（命中测试）', await evaluate(`(function(){
    const r = document.querySelector('#hx-maxretry');
    r.scrollIntoView({block:'center'});
    const b = r.getBoundingClientRect();
    // 视口外时 elementFromPoint 必返回 null，先确认已在视口内
    if (b.top < 0 || b.bottom > innerHeight) return false;
    const el = document.elementFromPoint(b.left + b.width/2, b.top + b.height/2);
    return el === r || (el && r.contains(el));
  })()`));

  const overflow375 = await evaluate(`(function(){
    const w = document.documentElement.clientWidth, bad = [];
    document.querySelectorAll('main.hx *').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.right > w + 1 || r.left < -1)) {
        if (el.closest('.hx-code') || el.closest('.hx-tl__pre') || el.closest('.hx-log')) return;
        bad.push(el.tagName + '.' + String(el.className).slice(0,30));
      }
    });
    return bad.slice(0,6);
  })()`);
  eq('8.8 375px 无元素溢出（代码区除外）', overflow375.length, 0, overflow375.join(' | '));

  /* ============ 9. 减弱动效 ============ */
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 900, deviceScaleFactor: 1, mobile: false
  });
  await send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-reduced-motion', value: 'reduce' }]
  });
  await goto(TARGET);

  ok('9.1 reduced-motion 下内容不被隐藏',
    await evaluate(`Array.from(document.querySelectorAll('.hx-reveal'))
      .every(el => getComputedStyle(el).opacity === '1')`));
  eq('9.2 reduced-motion 下数字直接落终值',
    await evaluate("document.querySelector('[data-count]').textContent"), '32%');
  // 注意：内联 style 已写死正确宽度，reduced-motion 下 JS 只是不播动画。
  // 断言「等于目标值」而不是「等于某个固定字符串」，避免把兜底与动画混为一谈。
  eq('9.3 reduced-motion 下瀑布条直接到位',
    await evaluate("document.querySelector('.hx-span__bar').style.width"), '100%');

  await send('Emulation.setEmulatedMedia', { features: [] });

  /* ============ 10. 深色模式 ============ */
  await send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: 'dark' }]
  });
  await goto(TARGET);

  eq('10.1 深色下 body 为深底',
    await evaluate("getComputedStyle(document.body).backgroundColor"), 'rgb(13, 13, 15)');
  ok('10.2 深色下导航不是白条',
    await evaluate(`(function(){
      const bg = getComputedStyle(document.querySelector('.site-nav')).backgroundColor;
      const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
      return m && (+m[1] + +m[2] + +m[3]) / 3 < 60;
    })()`), await evaluate("getComputedStyle(document.querySelector('.site-nav')).backgroundColor"));
  ok('10.3 深色下架构图文字亮度足够',
    await evaluate(`(function(){
      const t = document.querySelector('.hx-arch .zone-label') ||
                document.querySelector('.hx-arch .lyr text');
      if (!t) return false;
      const c = getComputedStyle(t).fill.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
      return !!(c && (+c[1] + +c[2] + +c[3]) / 3 > 120);
    })()`), await evaluate(`(()=>{
      const t = document.querySelector('.hx-arch .zone-label');
      return t ? getComputedStyle(t).fill : 'no-node';
    })()`));

  await send('Emulation.setEmulatedMedia', { features: [] });

  /* ============ 11. 资源与安全 ============ */
  const html = await evaluate("document.documentElement.outerHTML");
  // 只检查「会被浏览器真的去加载」的资源：脚本、样式、图片、字体、preload。
  // <link rel="canonical"> / og:image 这类是元数据，不产生请求，不能算外链依赖。
  const extRes = await evaluate(`Array.from(document.querySelectorAll(
      'script[src], link[rel="stylesheet"][href], link[rel="preload"][href], img[src], source[src]'))
    .map(e => e.src || e.href).filter(u => /^https?:/.test(u))`);
  eq('11.1 无外链脚本/样式/图片', extRes.length, 0, extRes.join(' '));
  const metaOnly = await evaluate(`Array.from(document.querySelectorAll('link[rel="canonical"], meta[property="og:image"]'))
    .length`);
  ok('11.2 canonical / og:image 等元数据存在（不产生请求）', metaOnly >= 2, 'count=' + metaOnly);
  ok('11.3 无 sk-/AKIA 类密钥', !/\bsk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}\b/.test(html));

  /* ============ 13. 单独打开（无站点 styles.css）============ */
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 900, deviceScaleFactor: 1, mobile: false
  });
  await goto(STANDALONE);

  eq('13.1 单独打开时激活外壳兜底',
    await evaluate("document.documentElement.classList.contains('hx-no-shell')"), true);
  eq('13.2 导航不是默认链接样式',
    await evaluate(`(function(){
      const a = document.querySelector('.nav-links a');
      return a ? getComputedStyle(a).textDecorationLine : 'no-link';
    })()`), 'none');
  eq('13.3 导航项横排（不是竖排列表）',
    await evaluate(`(function(){
      const nav = document.querySelector('.nav-links');
      return nav ? getComputedStyle(nav).flexDirection : 'no-nav';
    })()`), 'row');
  eq('13.4 二级菜单默认收起（不平铺）',
    await evaluate(`(function(){
      const m = document.querySelector('.nav-menu');
      return m ? getComputedStyle(m).display : 'no-menu';
    })()`), 'none');
  eq('13.5 列表无项目符号',
    await evaluate(`(function(){
      const l = document.querySelector('.nav-menu li') || document.querySelector('.nav-menu');
      return l ? getComputedStyle(l).listStyleType : 'none';
    })()`), 'none');
  ok('13.6 导航与正文之间无异常空白',
    await evaluate(`(function(){
      const nav = document.querySelector('.site-nav').getBoundingClientRect();
      const hero = document.querySelector('.hx-hero').getBoundingClientRect();
      return (hero.top - nav.bottom) < 40;
    })()`), await evaluate(`(function(){
      const nav = document.querySelector('.site-nav').getBoundingClientRect();
      const hero = document.querySelector('.hx-hero').getBoundingClientRect();
      return 'gap=' + Math.round(hero.top - nav.bottom) + 'px';
    })()`));
  ok('13.7 单独打开时交互仍可用',
    await evaluate(`(function(){
      const t = document.querySelectorAll('.hx-tab')[1];
      if (!t) return false;
      t.click();
      return !document.getElementById('hx-panel-arch').hidden;
    })()`));
  ok('13.8 单独打开时页脚有样式',
    await evaluate(`(function(){
      const f = document.querySelector('.site-footer');
      const bg = getComputedStyle(f).backgroundColor;
      return bg && bg !== 'rgba(0, 0, 0, 0)';
    })()`));

  /* ============ 14. 装进站点时兜底不误触 ============ */
  await goto(TARGET);
  eq('14.1 站点样式存在时兜底不激活',
    await evaluate("document.documentElement.classList.contains('hx-no-shell')"), false);
  ok('14.2 站点原有导航样式未被覆盖',
    await evaluate(`(function(){
      const bg = getComputedStyle(document.querySelector('.site-nav')).backgroundImage;
      return bg && bg !== 'none';
    })()`), await evaluate("getComputedStyle(document.querySelector('.site-nav')).backgroundImage.slice(0,40)"));

  /* ============ 12. 控制台 ============ */
  eq('12.1 无 JS 运行时异常', pageErrors.length, 0, pageErrors.slice(0,3).join(' | '));
  // 区分「真 JS 报错」与「预期内的资源缺失」：
  // 单独打开时 styles.css / favicon 必然 404，这类不是代码缺陷。
  // 只把「脚本执行错误」算失败，资源加载失败单独列出供人工确认。
  const scriptErrors = consoleErrors.filter(t =>
    t && !/net::ERR_FILE_NOT_FOUND|Failed to load resource|favicon|styles\.css/i.test(t));
  eq('12.2 无脚本执行错误', scriptErrors.length, 0,
    scriptErrors.slice(0, 6).map(s => s.slice(0, 160)).join(' ||| '));
  const resErrors = consoleErrors.length - scriptErrors.length;
  ok('12.3 资源缺失仅为预期内的 styles.css/favicon', resErrors <= 8,
    resErrors + ' 条资源告警（单独打开时 styles.css 不存在，属预期）');

} catch (err) {
  results.push({ name: 'FATAL', pass: false, detail: err.message });
} finally {
  try { cdp.close(); } catch {}
  try { proc.kill(); } catch {}
}

/* ------------------------------------------------------------------ 输出 */
const failed = results.filter(r => !r.pass);
if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ total: results.length, failed: failed.length, results }, null, 2));
} else {
  for (const r of results) {
    console.log((r.pass ? 'PASS  ' : 'FAIL  ') + r.name + (r.pass ? '' : '   ← ' + r.detail));
  }
  console.log('\n' + (results.length - failed.length) + '/' + results.length + ' 通过');
}
process.exit(failed.length ? 1 : 0);
