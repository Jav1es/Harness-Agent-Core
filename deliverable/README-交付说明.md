# Harness-Agent-Core · 交付与验收

> 生成：2026-10-22 · 模块编号 **24** · 归属组 **核心案例（core）** · 详情页 `harness-agent-core.html`

---

## 一、交付物清单

| 文件 | 大小 | 作用 | 是否进站点仓库 |
|---|---|---|---|
| `harness-agent-core.html` | 66 KB | 详情页（全部文案 + 内联 SVG 架构图 + 代码 Tab + 演示控件） | 是 |
| `harness-agent-core.css` | 44 KB | 详情页样式（复用站点 token，带独立预览兜底） | 是 |
| `harness-agent-core.js` | 36 KB | 详情页交互（canvas、分层、Tab、时间线、熔断、计数） | 是 |
| `harness-shell-fallback.css` | 12 KB | **外壳兜底**：`styles.css` 缺失时接管导航/页脚/按钮 | 是（建议） |
| `patch-harness-module.mjs` | 12 KB | 并入作品集的幂等补丁（改 works-data + HOME_PICK + 计数文案） | 否（一次性工具） |
| `verify.mjs` | 20 KB | 57 项自动断言（无头 Chrome + CDP） | 否（一次性工具） |

**两种打开方式都能看**：

- **直接双击 `harness-agent-core.html`**（本目录，没有 `styles.css`）→ JS 探测到站点样式缺失，给 `<html>` 加 `.hx-no-shell`，由 `harness-shell-fallback.css` 接管导航与页脚。不会出现默认蓝色下划线链接、项目符号或整块空白。
- **装进作品集**（有 `styles.css`）→ 探测到站点样式已生效，兜底类**不加**，站点原有玻璃导航样式完全不受影响。

**零外链**：不加载任何外部字体、CDN、图片、第三方脚本。唯一的外部 URL 是 `<link rel="canonical">` 与 `og:image`（纯元数据，不产生请求）。

---

## 二、怎么装进 jav1es.github.io

```powershell
cd "D:\Jav1e Flies\00-工作区\portfolio-publish"

# 1) 先干跑，确认它打算改什么
node "D:\Jav1e Flies\00-工作区\Harness-core\deliverable\patch-harness-module.mjs" --dry-run

# 2) 真写（会自动备份 *.bak-harness-<时间戳>）
node "D:\Jav1e Flies\00-工作区\Harness-core\deliverable\patch-harness-module.mjs"

# 3) 把三个页面文件复制到站点根目录
Copy-Item "D:\Jav1e Flies\00-工作区\Harness-core\deliverable\harness-agent-core.*" . -Force
```

**补丁实际改动**（幂等，可重复运行）：

1. `works-data.json` / `works-data.js`：新增模块 `no=24`，`group=core`，`cat=agent`，`star=true`
2. `index.html`：`HOME_PICK` 改为 `['24','23','04','13','16','11']` —— 新模块占旗舰位，`01` 移出首页精选（**仍在 works.html 完整目录**）
3. 六个页面的计数文案 `23 → 24`

**没有自动改、需要你决定的三处**：

- `sitemap.xml`：要不要收录这个详情页
- `README.md`：里面的「17 个模块 / 22 个作品」等叙述性数字，机器改容易改错语气
- `resume-data.js`：简历页的技能标签是否要加 Harness Engineering

---

## 三、验收清单（57 项自动断言 + 人工项）

自动断言全部通过：`node verify.mjs` → **57/57**

### 导航与入口
- [x] 首页精选第一张即 Harness 卡片（`#mod-24`），宽度与同排卡片一致（556px / 268px）
- [x] 卡片封面沿用站点 `neural` 母题线稿，与其它 23 张同构
- [x] works.html 收录为第 24 个模块，归入「① 核心案例」组（该组由 5 → 6 个）
- [x] 卡片链接 `ai-agent.html#mod-24`（走站点既有的「在岗位页查看」规则）
- [x] 筛选计数同步显示 `24 / 24 个模块`

### 两种打开形态
- [x] 单独双击 `harness-agent-core.html`（无 styles.css）→ 导航/页脚/按钮正常，非浏览器默认样式
- [x] 单独打开时导航横排、二级菜单默认收起、无项目符号、导航与正文间无异常空白
- [x] 单独打开时交互（Tab / 时间线 / 熔断 / 数字动画）依然可用
- [x] 装进站点（有 styles.css）→ 兜底**不激活**，站点原有玻璃导航样式不被覆盖

### 详情页
- [x] 路由 `harness-agent-core.html`（站点是静态多页，无 `/works/` 路由层，故按现有规则用平级 HTML）
- [x] SEO：`title` / `description` / `canonical` / OG / Twitter Card / JSON-LD `CreativeWork` 齐备
- [x] 七个章节按黄金叙事顺序：痛点 → 架构 → 五支柱 → 代码 → 指标 → 可观测 → 结语
- [x] 导航与页脚与站点其它页一致（含手机端汉堡菜单）

### 架构图
- [x] 内联 SVG，`role="img"` + `<title>` + `<desc>` 三件套
- [x] 三段结构完整：Frontier Model ⇄ Harness Layer（五层）⇄ Enterprise System
- [x] 顶部「模型直连系统」虚线被明确划掉（✕ + 说明文字）
- [x] 滚动进入视口自动巡览高亮五层；点击层卡片可锁定
- [x] 375px 下自动切换为**竖排三段 HTML 版**（不靠横向拖动才能看全）

### 代码片段
- [x] 四个函数齐全：`guard_tool_call` / `deterministic_validator` / `step_execution_with_harness` / `run_with_self_healing`
- [x] 每个函数带设计理由注释（为什么白名单在代码层、为什么回喂要带边界）
- [x] 三个 Tab：Python 核心片段 / 架构说明 / CI 门禁，支持 ←→ Home End 键盘切换
- [x] 复制按钮：clipboard API 优先，`file://` 下自动降级 `execCommand`
- [x] 无 HTML 实体泄漏（代码中的 `<` `>` 已正确转义并渲染为原字符）

### 指标
- [x] 四个数字：32% → <2%、−80%、98%，滚动进入时数字动画
- [x] 前后对比条（中断率 / 成功率两组）
- [x] **口径声明块**：数据来源、任务构成、中断定义、两口径同源、分母注意、不适用范围、成本口径
- [x] 分母自洽：50 次样本 → 0 次中断才是 <2%，1 次即 2%（已在页面写明）

### 交互
- [x] 自愈时间线：五步自动播放 + 可点击跳步 + 键盘 Enter/Space
- [x] 熔断演示：`max_retries=2` → `CIRCUIT BREAKER TRIGGERED`；`=3` → 自愈成功（两种路径都实测过）
- [x] 指标数字动画、Span 瀑布条错位、对比条生长
- [x] canvas 线框缰绳图：30fps 上限、DPR≤2、离屏暂停、页面隐藏暂停

### 移动端
- [x] 375px 无横向滚动（逐元素检测溢出源，0 处）
- [x] 架构图改竖排、时间线改竖排、KPI 两列、代码块带「可左右滑动」提示
- [x] 滑块命中测试通过（`elementFromPoint` 命中）
- [x] 导航抽屉可展开/收起，`aria-expanded` 同步

### 性能与无障碍
- [x] 零 `filter: blur()` / `backdrop-filter`（仅注释里提到，实际 0 处使用）
- [x] 动画只碰 `transform` / `opacity` / `width`
- [x] 进场动画由 JS 加 `.hx-armed`，未加类默认可见；2.5s 兜底强制显示
- [x] `prefers-reduced-motion`：不播自动循环、数字直接落终值、瀑布条直接到位
- [x] 深色模式：导航、架构图文字、canvas 线条均已验证可读
- [x] 无重复 id、无死锚点、无缺 alt 图片、Tab 组件 `aria-selected` 同步
- [x] 无 `sk-*` / `AKIA*` / Bearer / password 类凭据

---

## 四、已知局限（如实写在这里，也写在页面上）

1. **样本量小**：50 次测试的最小分辨率是 2 个百分点，`<2%` 实际等价于「0 次中断」。要谈「98%」这种精度，样本量应该在 200 次以上。
2. **断言覆盖不到的收益不成立**：Harness 的上限取决于你能把验收标准写得多确定。开放式任务（写文案、做设计）收益会明显小于代码/数据类任务。
3. **成本是真实增加的**：自愈会多花 Token（演示链路约 2,270 tokens/次），换的是人工返工时间。人工成本低于 Token 成本时这笔账不划算。
4. **可观测面板是演示数据**：结构与真实 trace 一致，但不是生产流量。
5. **代码是展示用精简版**：`jsonschema_errors()` / `lint_patch()` 是占位实现，不是可直接运行的完整库。

---

## 五、复验方式

```powershell
cd "D:\Jav1e Flies\00-工作区\Harness-core\deliverable"
node verify.mjs          # 57 项断言
node verify.mjs --json   # 机器可读
```

断言覆盖：控制台零报错、375px 零溢出、交互可用、减弱动效降级、深色模式可读、无外链资源、无密钥，以及**单独打开 vs 装进站点两种形态下外壳样式都正确**。

预览目录 `..\preview\` 是自带站点 `styles.css` 的独立副本，双击 `preview\index.html` 即可看完整效果，不必装进站点。
