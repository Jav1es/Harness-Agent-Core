/**
 * patch-harness-module.mjs
 * ---------------------------------------------------------------------------
 * 把 Harness-Agent-Core 模块并入 jav1es.github.io 作品集（portfolio-publish）。
 *
 * 幂等：可重复运行。已存在的内容会被识别并跳过或原位更新，不会重复插入。
 *
 * 做四件事：
 *   1. 把 works-data.json / works-data.js 里新增一条模块（no=24，group=core）
 *   2. 同步 works-data.js（内联版，供 file:// 双击打开）
 *   3. 在 index.html 的精选网格里追加一张首页卡片（data-hx-card 标记，幂等）
 *   4. 更新 works.html 与 index.html 的模块计数文案（23 → 24）
 *
 * 用法：
 *   cd "D:\Jav1e Flies\00-工作区\portfolio-publish"
 *   node patch-harness-module.mjs --dry-run   # 先看会改什么
 *   node patch-harness-module.mjs             # 真的写入
 *
 * 不做的事（需要你手工确认后再动）：
 *   · 不改 sitemap.xml（详情页是独立 HTML，是否收录由你定）
 *   · 不改 3D 场景（whale-3d.js / orbit-3d.js 会按数据长度自适应，无需改）
 *   · 不碰 README.md 的历史数字（那里是叙述性文字，机器改容易改错语气）
 */
import { readFileSync, writeFileSync, copyFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SITE = process.argv.includes('--site')
  ? process.argv[process.argv.indexOf('--site') + 1]
  : 'D:\\Jav1e Flies\\00-工作区\\portfolio-publish';

const DRY = process.argv.includes('--dry-run');
const MODULE_NO = '24';
const MARK = 'data-hx-card="harness-agent-core"';

const log = [];
function note(msg) { log.push(msg); }

/* ========================================================================== */
/* 1. 新模块数据                                                              */
/* ========================================================================== */
const NEW_ITEM = {
  no: MODULE_NO,
  t: 'Harness-Agent-Core：把不可控的 AI 变成可验收的交付',
  cat: 'agent',
  star: true,
  status: '框架设计 · 实验验证',
  jd: 'AI 智能体开发工程师',
  pitch:
    '直接调大模型做多步骤任务，失败率约三成：幻觉被当事实、工具越权、格式漂移、死循环烧 Token、出错后无法定位。' +
    '我做了一层代码级 Harness：模型只产出「提案」，能不能执行由白名单、确定性校验、自愈回喂与熔断共同决定。' +
    '在 50 次复杂多步骤测试中，任务中断率由 32% 降至 2% 以下。',
  highlights: [
    { num: '70% → 98%', label: '交付成功率（50 次实验口径）' },
    { num: '−80%', label: '错误修正平均耗时' },
    { num: '5', label: '确定性能力层' }
  ],
  tags: ['Harness Engineering', 'LangGraph', 'MCP', 'CI/CD Gate'],
  links: ['完整案例页|./harness-agent-core.html'],
  shot: '',
  metric:
    '作品集实验口径：50 次复杂多步骤测试，任务中断率 32% → <2%，错误修正平均耗时缩短 80%；非客户项目、非第三方基准',
  role:
    '独立完成框架设计与页面实现：五层能力模型、四个核心函数（工具白名单鉴权 / 确定性校验 / 错误栈回喂 / max_retries 熔断）、' +
    'CI 门禁配置与可观测面板设计。叙事与工程细节均可在详情页逐段核对。',
  arch:
    'Frontier Model ⇄ Harness Layer ⇄ Deterministic Enterprise System。Harness 分五层：' +
    'L1 工具编排与上下文隔离（MCP 白名单 + 权限边界）→ L2 确定性硬约束守卫（Schema / 业务规则 / Linter）→ ' +
    'L3 自愈反馈循环（error_stack 结构化回喂）→ L4 熔断与人工介入网关（max_retries + Webhook 审批）→ ' +
    'L5 全链路可观测与 CI 门禁（Token / 耗时 / Span / 回归断言）。',
  pain:
    '把前沿模型接上业务系统，Demo 一个下午就能跑通，上生产却要三个月——且大部分时间在补模型留下的坑：' +
    '幻觉被当成事实、提示词注入导致越权调用、输出格式漂移打崩下游、失败重试没有上限烧掉预算、出了问题没有 trace 可查。',
  steps: [
    '把「模型能做什么」与「系统允许做什么」拆成两层，后者写成代码而不是提示词',
    '定义 TOOL_POLICY 白名单：scopes、是否产生副作用、金额上限，未登记工具直接抛异常',
    '实现确定性校验器：JSON Schema → 业务规则 → Linter，三道关全部是纯函数，可单测可进 CI',
    '设计错误栈结构化回喂：不只给报错原文，还带 fix_hint 与 do_not_change 边界',
    '加 max_retries 熔断与高风险人工审批网关，额度耗尽即停手不追加 Token',
    '接入全链路 span 与 CI 回归门禁，断言检索准确率 / 引用准确率 / 拒答率',
    '用 50 次多步骤测试做前后对比，把中断率与修正耗时量化出来'
  ],
  hard:
    '难点有三处。一是「校验器必须是确定性的」——用 LLM 去校验 LLM 等于没校验，所以规则全部写成纯函数，' +
    '代价是规则要人工维护、覆盖不到开放式语义问题。二是「回喂要有边界」——只丢原始报错，模型会顺手重构，' +
    '改动面从 6 行变 60 行，回归风险翻倍；加上 do_not_change 后才收敛。' +
    '三是「熔断点的位置」——重试的边际收益下降很快，第 4 次之后基本是噪声，所以上限设在 3，' +
    '代价是少数本可在第 4 次成功的任务被转人工。',
  retro:
    '如果重做，会把「验收标准可写成断言」当成选任务的前置条件——任务越开放、断言越难写，Harness 的收益越小。' +
    '另外 50 次样本的分辨率只有 2 个百分点（0 次中断才是 <2%），样本量偏小是这套数字最主要的局限，' +
    '下一步应该扩到 200 次并加入延迟与成本维度。',
  group: 'core',
  motif: 'neural',
  boundary:
    '个人作品集实验，非客户项目、非生产环境流量；50 次测试由本人设计与执行，非第三方基准。' +
    '指标只说明在该测试集上的表现，不可外推为「任意任务都能到 98%」。页面中的可观测面板与熔断演示为演示数据，未接入真实系统。'
};

/* ========================================================================== */
/* 2. works-data.json                                                         */
/* ========================================================================== */
const dataPath = join(SITE, 'works-data.json');
if (!existsSync(dataPath)) {
  console.error('找不到 works-data.json：' + dataPath);
  process.exit(1);
}
const data = JSON.parse(readFileSync(dataPath, 'utf8'));

const existing = data.items.find(it => it.no === MODULE_NO);
if (existing && existing.t !== NEW_ITEM.t) {
  console.error(`编号 ${MODULE_NO} 已被「${existing.t}」占用。请改 MODULE_NO 后重跑。`);
  process.exit(1);
}

if (existing) {
  Object.assign(existing, NEW_ITEM);
  note(`works-data.json：更新已存在的模块 ${MODULE_NO}`);
} else {
  data.items.push(NEW_ITEM);
  data.items.sort((a, b) => String(a.no).localeCompare(String(b.no)));
  note(`works-data.json：新增模块 ${MODULE_NO}（当前共 ${data.items.length} 个）`);
}

// 让新模块进「核心案例」组（如果 groupOrder 里没有就补上）
if (!data.groupOrder.includes('core')) {
  data.groupOrder.unshift('core');
  note('works-data.json：groupOrder 补入 core');
}

/* ========================================================================== */
/* 3. works-data.js（内联版）                                                  */
/* ========================================================================== */
const dataJsPath = join(SITE, 'works-data.js');
const dataJs = readFileSync(dataJsPath, 'utf8');
const jsonStr = JSON.stringify(data);
// works-data.js 的形态是 window.__WORKS_DATA__ = {...};
const jsMatch = dataJs.match(/^([\s\S]*?=\s*)(\{[\s\S]*\})(\s*;?\s*)$/);
if (!jsMatch) {
  console.error('works-data.js 结构不是预期的 `window.__WORKS_DATA__ = {...};`，请手工处理。');
  process.exit(1);
}
const newDataJs = jsMatch[1] + jsonStr + jsMatch[3];
note('works-data.js：已用同一份数据重写（保证与 json 一致）');

/* ========================================================================== */
/* 4. index.html：把模块加进首页精选（HOME_PICK）                             */
/* ========================================================================== */
/* 为什么改 HOME_PICK 而不是插一张静态卡片：
   index.html 的精选网格由 WORKS.pickHTML(data.items, HOME_PICK) 渲染，
   卡片样式、封面母题（motif）、点击行为全部来自 works.js。
   手写一张静态卡会脱离这套渲染 —— 实测会出现类名冲突与宽度不一致。
   往 HOME_PICK 里加编号，卡片就和其它 6 张完全同构，不需要额外 CSS。 */
const indexPath = join(SITE, 'index.html');
let indexHtml = readFileSync(indexPath, 'utf8');

const pickRe = /var HOME_PICK = \[([^\]]*)\];/;
const pickMatch = indexHtml.match(pickRe);
if (!pickMatch) {
  note('index.html：未找到 HOME_PICK，首页精选未改动（需手工确认）');
} else {
  const cur = pickMatch[1].split(',').map(s => s.trim().replace(/['"]/g, '')).filter(Boolean);
  if (cur.includes(MODULE_NO)) {
    note('index.html：HOME_PICK 已包含 ' + MODULE_NO + '，跳过');
  } else {
    // 放到最前（旗舰位），并保持 6 张 —— 挤掉原本最后一张（01 规划包，仍在 works.html 完整目录里）
    const next = [MODULE_NO, ...cur].slice(0, cur.length);
    const dropped = cur.filter(no => !next.includes(no));
    indexHtml = indexHtml.replace(pickRe, 'var HOME_PICK = [' + next.map(n => `'${n}'`).join(', ') + '];');
    note('index.html：HOME_PICK = [' + next.join(', ') + ']' +
      (dropped.length ? '（移出：' + dropped.join(', ') + '，仍在 works.html 完整目录）' : ''));
  }
}
/* ========================================================================== */
/* 5. 计数文案 23 → 24                                                        */
/* ========================================================================== */
const countFiles = ['index.html', 'works.html', 'about.html', 'ai-agent.html', 'fde.html', 'vibe-engineer.html'];
let countChanged = 0;
for (const f of countFiles) {
  const p = join(SITE, f);
  if (!existsSync(p)) continue;
  let html = f === 'index.html' ? indexHtml : readFileSync(p, 'utf8');
  const before = html;
  // 只改「23 个模块」「23 个深度作品模块」这类明确计数，不碰年份与其它数字
  html = html.replace(/23 个深度作品模块/g, '24 个深度作品模块')
             .replace(/23 个模块/g, '24 个模块')
             .replace(/全部 23 个/g, '全部 24 个');
  if (html !== before) {
    countChanged++;
    if (f === 'index.html') indexHtml = html;
    else if (!DRY) writeFileSync(p, html, 'utf8');
    note(`${f}：计数文案 23 → 24`);
  }
}

/* ========================================================================== */
/* 6. 写入                                                                    */
/* ========================================================================== */
if (DRY) {
  console.log('=== DRY RUN（未写入任何文件）===');
  log.forEach(l => console.log('  · ' + l));
  console.log('\n去掉 --dry-run 即真正写入。');
} else {
  // 备份
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  for (const f of ['works-data.json', 'works-data.js', 'index.html']) {
    const src = join(SITE, f);
    if (existsSync(src)) copyFileSync(src, src + '.bak-harness-' + stamp);
  }
  writeFileSync(dataPath, JSON.stringify(data, null, 0), 'utf8');
  writeFileSync(dataJsPath, newDataJs, 'utf8');
  writeFileSync(indexPath, indexHtml, 'utf8');

  console.log('=== 已写入 ===');
  log.forEach(l => console.log('  · ' + l));
  console.log('\n备份后缀：.bak-harness-' + stamp);
  console.log('下一步：node build.mjs（如果项目有构建步骤），然后本地起服务验收。');
}
