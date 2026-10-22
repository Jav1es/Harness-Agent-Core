/* ==========================================================================
   Harness-Agent-Core · 详情页交互
   --------------------------------------------------------------------------
   零依赖、零外链、无后端。所有数据为页面内写死的演示数据。

   设计纪律（与 styles.css 一致）：
     · 动效只碰 transform / opacity / width（合成器友好）
     · 一切进场隐藏态由 JS 加类，JS 没跑起来时内容默认可见
     · prefers-reduced-motion 下：不播自动循环、数字直接落终值
     · 所有交互控件都有键盘路径与 aria 状态同步

   导出：window.HARNESS_AGENT_CORE.boot()  —— 幂等，可重复调用
   ========================================================================== */
(function (global) {
  'use strict';

  var HX = {};
  var booted = false;

  /* ---------------------------------------------------------------- 工具 */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function clamp(n, a, b) { return n < a ? a : n > b ? b : n; }

  var mqReduce = global.matchMedia
    ? global.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false, addEventListener: null, addListener: null };

  function reduced() { return !!(mqReduce && mqReduce.matches); }

  function onMotionChange(fn) {
    if (!mqReduce) return;
    if (mqReduce.addEventListener) mqReduce.addEventListener('change', fn);
    else if (mqReduce.addListener) mqReduce.addListener(fn);
  }

  function cssVar(el, name, fallback) {
    var v = getComputedStyle(el).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  /* ======================================================================
     1. 进场动画
     规则：只把「视口外」的元素武装成隐藏态；视口内的立刻可见。
     兜底：2.5s 后无论 IO 是否回调，强制全部显示。
     ====================================================================== */
  function initReveal(root) {
    var els = $$('.hx-reveal', root);
    if (!els.length) return;

    if (!('IntersectionObserver' in global) || reduced()) {
      els.forEach(function (el) { el.classList.add('hx-in'); });
      return;
    }

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add('hx-in');
          io.unobserve(e.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });

    els.forEach(function (el) {
      var r = el.getBoundingClientRect();
      var inView = r.top < global.innerHeight + 120 && r.bottom > -120;
      if (!inView) {
        el.classList.add('hx-armed');
        io.observe(el);
      } else {
        el.classList.add('hx-in');
      }
    });

    global.setTimeout(function () {
      $$('.hx-armed', root).forEach(function (el) { el.classList.add('hx-in'); });
    }, 2500);
  }

  /* ======================================================================
     2. Hero Canvas：线框「缰绳」节点图
     ----------------------------------------------------------------------
     左：模型（八面体线框，会缓慢自转）
     中：Harness 支架（双轨 + 五层刻度 + 校验闸门）
     右：确定性企业系统（三层堆叠）
     之间：一条贝塞尔「缰绳」，有数据点在轨道上单向流动

     性能：DPR 上限 2；离开视口即停；reduced-motion 只画静态一帧；
     帧率上限 30fps（这是装饰图，不需要 60fps）。
     ====================================================================== */
  function initHeroViz(canvas) {
    if (!canvas || !canvas.getContext) return;

    var ctx = canvas.getContext('2d');
    var host = canvas.closest('.hx') || document.documentElement;

    var W = 420, H = 300;
    var dpr = 1;
    var running = false, visible = true, rafId = 0, lastT = 0;
    var t0 = 0;

    var C = {};

    /* 颜色必须在「每次绘制前」从 CSS 变量读：主题切换后变量会变，
       缓存在闭包里就会留下浅色模式的颜色画在深色底上。
       另外深色底上线条与小字要单独抬一档 —— --hx-txt3(#8e8e94) 画在
       #1a1a1f 上只有 3.6:1，作为装饰线够用，作为标注文字不够。 */
    function isDark() {
      var set = document.documentElement.getAttribute('data-theme');
      if (set === 'dark') return true;
      if (set === 'light') return false;
      return !!(global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches);
    }

    function readColors() {
      var dark = isDark();
      C.line = dark ? '#c2c2c0' : cssVar(host, '--hx-txt3', '#6e6e6e');   // 主线
      C.faint = dark ? '#5c5c66' : cssVar(host, '--hx-line2', '#c9c9c2');  // 淡线
      C.txt = dark ? '#e8e8e6' : cssVar(host, '--hx-txt2', '#3d3d3d');
      C.dim = dark ? '#a8a8b0' : cssVar(host, '--hx-txt3', '#6e6e6e');
      C.blue = cssVar(host, '--hx-blue', '#2563eb');
      C.amber = cssVar(host, '--hx-amber', '#d97706');
      C.green = cssVar(host, '--hx-green', '#059669');
      C.bg = cssVar(host, '--hx-card2', '#fafaf8');
    }
    readColors();

    function resize() {
      dpr = clamp(global.devicePixelRatio || 1, 1, 2);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.aspectRatio = W + ' / ' + H;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    /* ---- 几何 ---- */
    var MODEL = { x: 74, y: 150, r: 30 };
    var SYS = { x: 348, y: 150, w: 64, h: 74 };
    var P0 = { x: 106, y: 150 };
    var P1 = { x: 160, y: 84 };
    var P2 = { x: 258, y: 216 };
    var P3 = { x: 312, y: 150 };

    function bez(t) {
      var u = 1 - t;
      return {
        x: u * u * u * P0.x + 3 * u * u * t * P1.x + 3 * u * t * t * P2.x + t * t * t * P3.x,
        y: u * u * u * P0.y + 3 * u * u * t * P1.y + 3 * u * t * t * P2.y + t * t * t * P3.y
      };
    }

    var TICKS = [0.18, 0.34, 0.5, 0.66, 0.82];
    var LAYER_NAMES = ['L1', 'L2', 'L3', 'L4', 'L5'];

    /* 八面体顶点（单位球 → 屏幕） */
    var OCTA = [
      [0, -1, 0], [1, 0, 0], [0, 0, 1], [-1, 0, 0], [0, 0, -1], [0, 1, 0]
    ];
    var EDGES = [
      [0, 1], [0, 2], [0, 3], [0, 4],
      [5, 1], [5, 2], [5, 3], [5, 4],
      [1, 2], [2, 3], [3, 4], [4, 1]
    ];

    function drawModel(angle) {
      var cosA = Math.cos(angle), sinA = Math.sin(angle);
      var pts = OCTA.map(function (p) {
        // 绕 Y 轴旋转 → 轻微 X 轴倾斜
        var x = p[0] * cosA - p[2] * sinA;
        var z = p[0] * sinA + p[2] * cosA;
        var y = p[1] * 0.86 + z * 0.32;
        return {
          x: MODEL.x + x * MODEL.r,
          y: MODEL.y + y * MODEL.r,
          z: z
        };
      });

      ctx.lineWidth = 1;
      EDGES.forEach(function (e) {
        var a = pts[e[0]], b = pts[e[1]];
        var depth = (a.z + b.z) / 2;
        ctx.strokeStyle = depth > 0 ? C.line : C.faint;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      });

      // 顶点
      pts.forEach(function (p) {
        if (p.z <= 0) return;
        ctx.fillStyle = C.blue;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.7, 0, Math.PI * 2);
        ctx.fill();
      });

      // 标注
      ctx.fillStyle = C.dim;
      ctx.font = '10px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('FRONTIER MODEL', MODEL.x, MODEL.y + MODEL.r + 26);
      ctx.fillStyle = C.txt;
      ctx.font = '600 9.5px ui-monospace, Consolas, monospace';
      ctx.fillText('非确定性', MODEL.x, MODEL.y + MODEL.r + 40);
    }

    function drawSystem() {
      var x = SYS.x - SYS.w / 2, y = SYS.y - SYS.h / 2;
      var slabH = 18, gap = 9;

      for (var i = 0; i < 3; i++) {
        var sy = y + i * (slabH + gap);
        ctx.strokeStyle = C.line;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect
          ? ctx.roundRect(x, sy, SYS.w, slabH, 3)
          : ctx.rect(x, sy, SYS.w, slabH);
        ctx.stroke();

        // 内部两条刻度线，暗示「结构化」
        ctx.strokeStyle = C.faint;
        ctx.beginPath();
        ctx.moveTo(x + 8, sy + slabH / 2);
        ctx.lineTo(x + SYS.w - 8, sy + slabH / 2);
        ctx.stroke();
      }

      // 校验通过的绿点（最底下一层）
      ctx.fillStyle = C.green;
      ctx.beginPath();
      ctx.arc(x + SYS.w - 9, y + 2 * (slabH + gap) + slabH / 2, 2, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = C.dim;
      ctx.font = '10px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('DETERMINISTIC', SYS.x, y + SYS.h + 20);
      ctx.fillStyle = C.txt;
      ctx.font = '600 9.5px ui-monospace, Consolas, monospace';
      ctx.fillText('企业系统', SYS.x, y + SYS.h + 34);
    }

    function drawHarness(angle) {
      // 双轨支架
      var top = 58, bot = 242, left = 196, right = 224;
      ctx.strokeStyle = C.faint;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, top); ctx.lineTo(left, bot);
      ctx.moveTo(right, top); ctx.lineTo(right, bot);
      ctx.stroke();

      // 五层刻度
      TICKS.forEach(function (t, i) {
        var p = bez(t);
        ctx.strokeStyle = C.line;
        ctx.beginPath();
        ctx.moveTo(p.x - 5, p.y);
        ctx.lineTo(p.x + 5, p.y);
        ctx.stroke();

        ctx.fillStyle = C.dim;
        ctx.font = '8.5px ui-monospace, Consolas, monospace';
        ctx.textAlign = 'right';
        ctx.fillText(LAYER_NAMES[i], p.x - 9, p.y + 3);
      });

      // 校验闸门：t=0.5 处一条竖虚线
      var g = bez(0.5);
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = C.amber;
      ctx.globalAlpha = 0.75;
      ctx.beginPath();
      ctx.moveTo(g.x, g.y - 34);
      ctx.lineTo(g.x, g.y + 34);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = C.amber;
      ctx.font = '600 8.5px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('GATE', g.x, g.y - 40);

      // 顶部标注
      ctx.fillStyle = C.txt;
      ctx.font = '600 10px ui-monospace, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.fillText('HARNESS', 210, 40);
      ctx.fillStyle = C.dim;
      ctx.font = '9px ui-monospace, Consolas, monospace';
      ctx.fillText('guard · retry · route', 210, 52);
    }

    function drawRein() {
      ctx.strokeStyle = C.line;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      for (var i = 0; i <= 60; i++) {
        var p = bez(i / 60);
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();
    }

    function drawDots(time) {
      var n = 4;
      for (var i = 0; i < n; i++) {
        var speed = 0.00016;
        var t = ((time * speed) + i / n) % 1;
        var p = bez(t);

        // 过闸门瞬间闪一下（视觉上「被校验」）
        var nearGate = Math.abs(t - 0.5) < 0.045;
        ctx.fillStyle = nearGate ? C.amber : C.blue;
        ctx.globalAlpha = nearGate ? 1 : 0.85;
        ctx.beginPath();
        ctx.arc(p.x, p.y, nearGate ? 3.2 : 2.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    function frame(now) {
      if (!running) return;
      if (now - lastT < 33) { rafId = requestAnimationFrame(frame); return; } // ~30fps
      lastT = now;

      readColors();   // 每帧重读，主题切换即时生效
      var time = now - t0;
      var angle = reduced() ? 0.6 : time * 0.00042;

      ctx.clearRect(0, 0, W, H);

      drawRein();
      drawHarness(angle);
      drawModel(angle);
      drawSystem();
      if (!reduced()) drawDots(time);

      rafId = requestAnimationFrame(frame);
    }

    function start() {
      if (running || reduced() || !visible) return;
      running = true;
      t0 = performance.now() - 1200;
      lastT = 0;
      rafId = requestAnimationFrame(frame);
    }

    function stop() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
    }

    function staticFrame() {
      readColors();
      ctx.clearRect(0, 0, W, H);
      drawRein();
      drawHarness(0.6);
      drawModel(0.6);
      drawSystem();
      drawDots(1200);
    }

    resize();
    if (reduced()) staticFrame();
    else { staticFrame(); start(); }

    // 离屏暂停
    if ('IntersectionObserver' in global) {
      new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          visible = e.isIntersecting;
          if (visible) start(); else stop();
        });
      }, { threshold: 0.05 }).observe(canvas);
    }

    // 页面不可见时暂停
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stop();
      else if (visible) start();
    });

    onMotionChange(function () {
      stop();
      if (reduced()) staticFrame(); else start();
    });

    // 主题切换：站点的主题按钮改的是 <html data-theme>，canvas 必须跟着重画
    canvas.addEventListener('hx:theme', function () {
      if (reduced()) { staticFrame(); }
      else { stop(); start(); }
    });

    // 兜底：站点的主题按钮不一定派发 hx:theme（那是我们自己的约定），
    // 用 MutationObserver 监听 data-theme 变化，保证任何切换路径都能重绘。
    if (global.MutationObserver) {
      new MutationObserver(function () {
        if (reduced()) staticFrame();
        else { stop(); start(); }
      }).observe(document.documentElement, {
        attributes: true, attributeFilter: ['data-theme', 'class']
      });
    }

    var rt;
    global.addEventListener('resize', function () {
      clearTimeout(rt);
      rt = setTimeout(function () {
        resize();
        if (reduced() || !running) staticFrame();
      }, 180);
    });
  }

  /* ======================================================================
     3. 架构图：分层高亮（滚动进入自动巡览 + 手动点击锁定）
     ====================================================================== */
  function initArch(root) {
    var arch = $('.hx-arch', root);
    if (!arch) return;

    var svgGroups = $$('.lyr', arch);
    var mobileLayers = $$('.hx-am__layer', arch);
    var cards = $$('.hx-layer', root);
    if (!svgGroups.length && !mobileLayers.length) return;
    if (!cards.length) return;

    var idx = -1;
    var userLocked = false;
    var timer = 0;

    function paint(i) {
      idx = i;
      svgGroups.forEach(function (g, n) { g.classList.toggle('is-on', n === i); });
      mobileLayers.forEach(function (g, n) { g.classList.toggle('is-on', n === i); });
      cards.forEach(function (c, n) {
        c.classList.toggle('is-on', n === i);
        c.setAttribute('aria-pressed', n === i ? 'true' : 'false');
      });
    }

    function cycle() {
      if (userLocked) return;
      paint((idx + 1) % cards.length);
    }

    function play() {
      if (timer || reduced() || userLocked) return;
      arch.classList.add('is-playing');
      cycle();
      timer = setInterval(cycle, 2100);
    }

    function pause() {
      arch.classList.remove('is-playing');
      if (timer) { clearInterval(timer); timer = 0; }
    }

    // 点击锁定
    cards.forEach(function (card, i) {
      card.addEventListener('click', function () {
        userLocked = true;
        pause();
        paint(idx === i ? -1 : i);
      });
    });

    // 进入视口才开始巡览；离开即停
    if ('IntersectionObserver' in global) {
      new IntersectionObserver(function (es) {
        es.forEach(function (e) { if (e.isIntersecting) play(); else pause(); });
      }, { threshold: 0.35 }).observe(arch);
    } else {
      play();
    }
  }

  /* ======================================================================
     4. 代码 Tab
     ====================================================================== */
  function initTabs(root) {
    var tablist = $('.hx-tabs', root);
    if (!tablist) return;

    var tabs = $$('.hx-tab', tablist);
    var panels = tabs.map(function (t) {
      return document.getElementById(t.getAttribute('aria-controls'));
    });
    if (!tabs.length) return;

    function select(i, focus) {
      tabs.forEach(function (t, n) {
        var on = n === i;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        if (panels[n]) panels[n].hidden = !on;
      });
      if (focus) tabs[i].focus();
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(i, false); });
      t.addEventListener('keydown', function (e) {
        var k = e.key, next = -1;
        if (k === 'ArrowRight') next = (i + 1) % tabs.length;
        else if (k === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
        else if (k === 'Home') next = 0;
        else if (k === 'End') next = tabs.length - 1;
        if (next >= 0) { e.preventDefault(); select(next, true); }
      });
    });

    select(0, false);
  }

  /* ======================================================================
     5. 复制代码
     ====================================================================== */
  function initCopy(root) {
    $$('.hx-copy', root).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var box = btn.closest('.hx-codebox');
        var code = box && box.querySelector('code');
        if (!code) return;
        var text = code.innerText;
        var label = btn.getAttribute('data-label') || '复制';
        var ok = btn.getAttribute('data-ok') || '已复制';
        var fail = btn.getAttribute('data-fail') || '复制失败';

        function done(success) {
          btn.textContent = success ? ok : fail;
          setTimeout(function () { btn.textContent = label; }, 1800);
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { done(true); })
            .catch(function () { done(legacy(text)); });
        } else {
          done(legacy(text));
        }

        function legacy(t) {
          var ta = document.createElement('textarea');
          ta.value = t;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.top = '-1000px';
          document.body.appendChild(ta);
          ta.select();
          var r = false;
          try { r = document.execCommand('copy'); } catch (e) { r = false; }
          document.body.removeChild(ta);
          return r;
        }
      });
    });
  }

  /* ======================================================================
     6. 自愈时间线
     ====================================================================== */
  var TL_STEPS = [
    {
      key: 'error',
      title: 'Error Stack',
      sub: 'step 3 / 5',
      meta: ['attempt=1', 'max_retries=3', 'elapsed=4.2s', 'tokens=1,284'],
      body:
        '<span class="dim">$ pytest tests/test_settlement.py -q</span>\n' +
        '<span class="bad">FAILED</span> tests/test_settlement.py::test_rounding\n' +
        '  AssertionError: 12.34 != 12.35\n' +
        '    <span class="dim">expected 12.35, got 12.34</span>\n' +
        '    <span class="dim">at settlement.py:88 in settle()</span>\n' +
        '<span class="dim">1 failed, 11 passed in 0.84s</span>',
      cap: '模型这一轮生成了可用代码，但金额四舍五入没走银行的 half-up 规则。这不是「模型不行」，是没人告诉它失败在哪。'
    },
    {
      key: 'gate',
      title: 'Validator 拦截',
      sub: 'deterministic',
      meta: ['rule=settlement.rounding', 'schema=ok', 'verdict=REJECT'],
      body:
        '<span class="dim">HARNESS › deterministic_validator(state)</span>\n' +
        '<span class="bad">✗ lint</span>   amount = round(x, 2)  <span class="dim">← 禁止使用 Python 默认 banker\'s rounding</span>\n' +
        '<span class="bad">✗ schema</span> missing field "currency"  <span class="dim">← required by settlement.v2</span>\n' +
        '<span class="good">✓ policy</span> 金额 ≤ 100,000  <span class="dim">(未触发人工审批)</span>\n' +
        'verdict = REJECT  →  不写入任何业务系统',
      cap: '拦截发生在写库之前。模型输出在这里只是一份「提案」，通过确定性校验才算数。'
    },
    {
      key: 'feed',
      title: 'Prompt 回喂',
      sub: 'error → context',
      meta: ['inject=error_stack', 'history=3 turns', 'delta=+412 tokens'],
      body:
        '<span class="dim">// harness 把失败现场结构化，而不是把错误原文丢回去</span>\n' +
        '{"attempt": 1,\n' +
        ' "failed_step": "test_rounding",\n' +
        ' "error": "AssertionError: 12.34 != 12.35",\n' +
        ' "rules_hit": ["settlement.rounding", "schema.currency"],\n' +
        ' "fix_hint": "use Decimal(...).quantize(ROUND_HALF_UP)",\n' +
        ' "do_not_change": ["public api", "db schema"]}',
      cap: '关键在 fix_hint 与 do_not_change：既给方向，也划出这一轮不许动的东西，避免模型「顺手重构」把范围放大。'
    },
    {
      key: 'retry',
      title: 'Retry',
      sub: 'attempt 2 / 3',
      meta: ['attempt=2', 'elapsed=3.1s', 'tokens=986', 'diff=+6 −4'],
      body:
        '<span class="dim">HARNESS › retry_with(state, feedback)</span>\n' +
        '<span class="good">+</span> from decimal import Decimal, ROUND_HALF_UP\n' +
        '<span class="good">+</span> amount = Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)\n' +
        '<span class="good">+</span> payload["currency"] = "CNY"\n' +
        '<span class="dim">  ...</span>\n' +
        '<span class="dim">$ pytest tests/test_settlement.py -q</span>\n' +
        '<span class="good">12 passed in 0.79s</span>',
      cap: '第二轮只改了 6 行。改动面越小，回归风险越低——这也是「回喂要带约束」的原因。'
    },
    {
      key: 'pass',
      title: 'Pass',
      sub: 'gate green',
      meta: ['attempt=2', 'self_healed=1', 'tokens_total=2,270', 'wall=7.3s'],
      body:
        '<span class="good">✓ unit</span>      12 passed\n' +
        '<span class="good">✓ schema</span>    settlement.v2 valid\n' +
        '<span class="good">✓ policy</span>    no restricted field touched\n' +
        '<span class="good">✓ ci-gate</span>   agent-regression 30/30\n' +
        '\n' +
        '<span class="good">RESULT: 交付通过，写入企业系统，trace_id 已归档</span>',
      cap: '整条链路的产出是「可验收的交付」，不是「一段看起来对的回答」。人只需要看最后这五行。'
    }
  ];

  function initTimeline(root) {
    var box = $('.hx-tl', root);
    if (!box) return;

    var steps = $$('.hx-step', box);
    var pre = $('.hx-tl__pre', box);
    var meta = $('.hx-tl__meta', box);
    var cap = $('.hx-tl__cap', box);
    var btn = $('[data-hx-play]', box);
    if (!steps.length || !pre) return;

    var cur = -1;
    var timer = 0;

    function render(i) {
      cur = i;
      var s = TL_STEPS[i];
      if (!s) return;

      steps.forEach(function (el, n) {
        el.classList.toggle('is-active', n === i);
        el.classList.toggle('is-done', n < i);
        el.classList.toggle('is-pass', n === i && s.key === 'pass');
      });

      pre.innerHTML = s.body;
      if (meta) {
        meta.innerHTML = s.meta.map(function (m) {
          var p = m.split('=');
          return p.length === 2
            ? '<span>' + p[0] + '=<b>' + p[1] + '</b></span>'
            : '<span>' + m + '</span>';
        }).join('');
      }
      if (cap) cap.textContent = s.cap;
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = 0; }
      if (btn) btn.textContent = btn.getAttribute('data-label-play') || '▶ 播放自愈过程';
    }

    function play() {
      stop();
      var i = 0;
      render(0);
      if (reduced()) return;   // 减弱动效：只落第一帧，用户自己点
      timer = setInterval(function () {
        i += 1;
        if (i >= TL_STEPS.length) { stop(); render(TL_STEPS.length - 1); return; }
        render(i);
      }, 1500);
    }

    steps.forEach(function (el, i) {
      // 步骤本身可点：想直接看某一步
      el.tabIndex = 0;
      el.setAttribute('role', 'button');
      el.setAttribute('aria-label', '查看第 ' + (i + 1) + ' 步：' + TL_STEPS[i].title);
      el.addEventListener('click', function () { stop(); render(i); });
      el.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); stop(); render(i); }
      });
    });

    if (btn) btn.addEventListener('click', play);

    render(0);

    // 进入视口自动播一次
    if ('IntersectionObserver' in global && !reduced()) {
      var once = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (e.isIntersecting) { play(); once.disconnect(); }
        });
      }, { threshold: 0.3 });
      once.observe(box);
    }
  }

  /* ======================================================================
     7. 熔断演示
     语义：脚本化场景 = 前两次必然失败、第三次必然成功。
     因此 max_retries ≥ 3 会自愈成功；< 3 会触发 Circuit Breaker。
     这正是要演示的东西：熔断不是兜底「让它过」，而是兜底「别烧 token」。
     ====================================================================== */
  function initBreaker(root) {
    var box = $('.hx-breaker', root);
    if (!box) return;

    var range = $('input[type="range"]', box);
    var out = $('output', box);
    var log = $('.hx-log', box);
    var state = $('.hx-breaker__state', box);
    var runBtn = $('[data-hx-run]', box);
    var resetBtn = $('[data-hx-reset]', box);
    if (!range || !log) return;

    var busy = false;
    var timers = [];

    function clearTimers() {
      timers.forEach(clearTimeout);
      timers = [];
      busy = false;
    }

    function line(html, cls) {
      var div = document.createElement('div');
      if (cls) div.className = cls;
      div.innerHTML = html;
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    }

    function setState(kind, text) {
      if (!state) return;
      state.classList.toggle('is-open', kind === 'open');
      state.innerHTML = '<i aria-hidden="true"></i>' + text;
    }

    function syncOut() {
      if (out) out.textContent = range.value;
      range.setAttribute('aria-valuetext', 'max_retries = ' + range.value);
    }

    range.addEventListener('input', syncOut);
    syncOut();

    function run() {
      if (busy) return;
      clearTimers();
      busy = true;
      log.innerHTML = '';
      setState('closed', 'CLOSED · 熔断器闭合');

      var max = parseInt(range.value, 10) || 1;
      line('<span class="l-dim">$ harness run --task settlement-fix --max_retries ' + max + '</span>');

      var attempt = 0;
      var totalTokens = 0;

      function step() {
        attempt += 1;
        var tokens = 640 + Math.floor(Math.random() * 60) * 10;
        totalTokens += tokens;

        if (attempt === 1) {
          line('<span class="l-dim">[attempt ' + attempt + '/' + max + ']</span> ' +
               'agent → patch settlement.py  <span class="l-dim">' + tokens + ' tokens</span>');
          line('<span class="l-warn">✗ gate</span> AssertionError: 12.34 != 12.35  <span class="l-dim">→ error_stack captured</span>');
        } else if (attempt === 2) {
          line('<span class="l-dim">[attempt ' + attempt + '/' + max + ']</span> ' +
               'agent → patch settlement.py  <span class="l-dim">' + tokens + ' tokens</span>');
          line('<span class="l-warn">✗ gate</span> lint: ROUND_HALF_UP missing  <span class="l-dim">→ error_stack captured</span>');
        } else {
          line('<span class="l-dim">[attempt ' + attempt + '/' + max + ']</span> ' +
               'agent → patch settlement.py  <span class="l-dim">' + tokens + ' tokens</span>');
          line('<span class="l-ok">✓ gate</span> 12 passed · schema ok · policy ok');
          line('<span class="l-ok">RESULT</span> self_healed=1 · attempts=' + attempt +
               ' · tokens=' + totalTokens.toLocaleString('en-US'));
          setState('closed', 'CLOSED · 自愈成功，未触发熔断');
          busy = false;
          return;
        }

        if (attempt >= max) {
          line('<span class="l-err">CIRCUIT BREAKER TRIGGERED</span> max_retries=' + max +
               ' 已耗尽，任务中止');
          line('<span class="l-dim">→ 挂起并通知人工：webhook 已发出 · 已消耗 ' +
               totalTokens.toLocaleString('en-US') + ' tokens 不再追加</span>');
          setState('open', 'OPEN · Circuit Breaker Triggered');
          busy = false;
          return;
        }

        line('<span class="l-dim">↻ 回喂 error_stack → 下一轮 prompt（含 do_not_change 约束）</span>');
        timers.push(setTimeout(step, 620));
      }

      timers.push(setTimeout(step, 260));
    }

    function reset() {
      clearTimers();
      log.innerHTML = '<span class="l-dim">待运行。拖动上方 max_retries 后点「运行演示」。</span>';
      setState('closed', 'CLOSED · 熔断器闭合');
    }

    if (runBtn) runBtn.addEventListener('click', run);
    if (resetBtn) resetBtn.addEventListener('click', reset);
    reset();
  }

  /* ======================================================================
     8. 数字动画 + 对比条 + Span 瀑布
     ====================================================================== */
  function initCounters(root) {
    var els = $$('[data-count]', root);
    if (!els.length) return;

    function run(el) {
      var to = parseFloat(el.getAttribute('data-count'));
      var dec = parseInt(el.getAttribute('data-dec') || '0', 10);
      var prefix = el.getAttribute('data-prefix') || '';
      var suffix = el.getAttribute('data-suffix') || '';

      function paint(v) {
        el.textContent = prefix + v.toFixed(dec) + suffix;
      }

      if (reduced()) { paint(to); return; }

      var dur = 900, start = 0;
      function tick(now) {
        if (!start) start = now;
        var p = clamp((now - start) / dur, 0, 1);
        var e = 1 - Math.pow(1 - p, 3);   // easeOutCubic
        paint(to * e);
        if (p < 1) requestAnimationFrame(tick);
        else paint(to);
      }
      requestAnimationFrame(tick);
    }

    if (!('IntersectionObserver' in global)) { els.forEach(run); return; }

    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        run(e.target);
        io.unobserve(e.target);
      });
    }, { threshold: 0.4 });

    els.forEach(function (el) { io.observe(el); });
  }

  function initBars(root) {
    // 两类条：对比条（data-w）与瀑布条（data-w + data-left）。
    // 注意：HTML 里已经写了内联 style 作为兜底宽度，所以「条有长度」不依赖 JS。
    // 这里只做「从 0 长到目标值」的动画，动画起点由 JS 自己设置再复位，
    // 一旦 IO 不回调，最坏情况是条一直是满的（正确值），而不是 0 宽（误导）。
    var bars = $$('[data-w]', root);
    if (!bars.length) return;

    function fill(el) {
      var w = el.getAttribute('data-w');
      var left = el.getAttribute('data-left');

      function apply() {
        el.style.width = w + '%';
        // 瀑布图靠 left 错位，不设的话所有 span 都会从 0 起跑
        if (left !== null) el.style.left = left + '%';
      }

      if (reduced()) { apply(); return; }

      // 先归零再赋值，才有「生长」动效；归零发生在元素已经可见时，
      // 若浏览器跳过过渡也只是从 0 直接跳到终值，不会永久停在 0。
      el.style.width = '0%';
      requestAnimationFrame(function () {
        requestAnimationFrame(apply);
      });
    }

    if (!('IntersectionObserver' in global)) { bars.forEach(fill); return; }

    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        fill(e.target);
        io.unobserve(e.target);
      });
    }, { threshold: 0.3 });

    bars.forEach(function (el) { io.observe(el); });
  }

  /* ======================================================================
     9. 站点外壳探测
     ----------------------------------------------------------------------
     详情页的外壳（导航/页脚/按钮）样式来自站点的 styles.css。
     如果三个文件被单独打开（styles.css 不存在），外壳会退化成浏览器默认样式：
     蓝色下划线链接、项目符号、整块留白。

     探测方法：读 .site-nav 的计算样式。
       · 站点样式生效时：position 为 sticky（styles.css 里写死了）
       · 样式缺失时：position 为 static（浏览器默认）
     命中「缺失」才给 <html> 加 .hx-no-shell，由 harness-shell-fallback.css 接管。
     这样单开能看、装进站点不受影响。
     ====================================================================== */
  function initShellFallback() {
    var nav = document.querySelector('.site-nav');
    if (!nav) return;

    var pos = getComputedStyle(nav).position;
    var isFallback = pos !== 'sticky' && pos !== 'fixed';

    // 站点样式表可能在 JS 之后才应用（defer 场景），再确认一次
    if (isFallback) {
      document.documentElement.classList.add('hx-no-shell');
    }
  }

  /* ======================================================================
     10. 导航兜底
     站点自带 menu.js；单独打开时没有，这里补一个最小可用的抽屉。
     站点 menu.js 已绑定时会打 data-hx-bound，避免重复绑定。
     ====================================================================== */
  function initNavFallback() {
    var toggle = $('[data-nav-toggle]');
    var links = document.getElementById('nav-links');
    if (!toggle || !links || links.hasAttribute('data-hx-bound')) return;
    links.setAttribute('data-hx-bound', '1');

    toggle.addEventListener('click', function () {
      var open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
      links.classList.toggle('is-open', !open);
    });

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (toggle.getAttribute('aria-expanded') !== 'true') return;
      toggle.setAttribute('aria-expanded', 'false');
      links.classList.remove('is-open');
      toggle.focus();
    });
  }

  function initTheme() {
    var btn = $('[data-hx-theme]');
    if (!btn) return;

    var KEY = 'theme';
    var root = document.documentElement;

    function current() {
      var set = root.getAttribute('data-theme');
      if (set === 'dark' || set === 'light') return set;
      return global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light';
    }

    function paint() {
      var isDark = current() === 'dark';
      btn.textContent = isDark ? '☀' : '☾';
      btn.setAttribute('aria-label', isDark ? '切换到浅色主题' : '切换到深色主题');
      btn.setAttribute('title', isDark ? '切换到浅色主题' : '切换到深色主题');
    }

    btn.addEventListener('click', function () {
      var next = current() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式下忽略 */ }
      paint();
      // 主题变了要重画 canvas（颜色是读 CSS 变量算的）
      var c = $('.hx-hero__viz canvas');
      if (c) {
        c.dispatchEvent(new CustomEvent('hx:theme', { bubbles: true }));
      }
    });

    paint();
  }

  /* ======================================================================
     11. 启动
     ====================================================================== */
  HX.boot = function (root) {
    root = root || document;
    if (booted) return;
    booted = true;

    // 外壳探测必须最先跑：它决定 harness-shell-fallback.css 是否接管
    initShellFallback();
    initNavFallback();
    initTheme();
    initReveal(root);

    var canvas = $('.hx-hero__viz canvas', root);
    if (canvas) initHeroViz(canvas);

    initArch(root);
    initTabs(root);
    initCopy(root);
    initTimeline(root);
    initBreaker(root);
    initCounters(root);
    initBars(root);
  };

  global.HARNESS_AGENT_CORE = HX;

  // 页面脚本已 defer，DOM 已就绪
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { HX.boot(); });
  } else {
    HX.boot();
  }
})(window);
