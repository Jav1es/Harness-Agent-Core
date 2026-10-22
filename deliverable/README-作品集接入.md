# Harness-Agent-Core · 作品集接入说明

> 生成：2026-10-22 · 仓库 **https://github.com/Jav1es/Harness-Agent-Core**
> 模块编号 **24** · 归属组 **核心案例（core）** · 详情页 `harness-agent-core.html`
> 本文件是**操作手册**，不是文案。文案在 `harness-agent-core.html` 里。

---

## 一、这次交付了什么

| 东西 | 位置 | 说明 |
|---|---|---|
| **真实可跑的 Python 包** | https://github.com/Jav1es/Harness-Agent-Core | 90 个文件，`pip install -e .` 后 `pytest` 597 全过 |
| **作品集详情页** | `deliverable/harness-agent-core.{html,css,js}` | 双击可看，也可装进站点 |
| **外壳兜底样式** | `deliverable/harness-shell-fallback.css` | 站点 `styles.css` 缺失时接管导航/页脚 |
| **并入站点的补丁** | `deliverable/patch-harness-module.mjs` | 幂等，改 works-data + HOME_PICK + 计数 |
| **页面断言脚本** | `deliverable/verify.mjs` | 57 项自动断言（无头 Chrome + CDP） |

**关键区别**：上次交付的是一个**页面**。这次页面背后有一个**能装能跑的仓库**。
详情页上的每个数字（597 测试、82 变异体、24 个源文件）都来自真实运行，可以复现：

```bash
git clone https://github.com/Jav1es/Harness-Agent-Core.git
cd Harness-Agent-Core
pip install -e .
pytest -q                                  # 597 passed
harness-core eval                          # 10/10, exit 0
python artifacts/mutation_check.py         # 82/82 mutants killed
```

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

## 三、详情页要加的一处内容（本轮新增）

详情页上「项目」相关的位置，**建议加一个 GitHub 链接**。上次没有仓库可链，这次有了：

```html
<a href="https://github.com/Jav1es/Harness-Agent-Core"
   target="_blank" rel="noopener"
   class="hx-repo-link">
  在 GitHub 上查看源码 →
</a>
```

三个可选位置，按视觉轻重排序：

| 位置 | 效果 | 建议 |
|---|---|---|
| 页首 hero 区标题下方 | 第一屏就能点 | **推荐** |
| 「交付物」或「验收」小节末尾 | 读完证据后给出口 | 次选 |
| 页脚 | 最不打扰 | 备选 |

**配套文案**（可直接用）：

> 597 个测试、82 个变异体全部被杀死、`mypy --strict` 零问题。
> 零运行时依赖，`pip install -e .` 之后 `pytest` 直接跑通，不需要任何密钥或网络。

---

## 四、验收清单

### 自动断言（页面）

```powershell
cd "D:\Jav1e Flies\00-工作区\Harness-core\deliverable"
node verify.mjs          # 57/57
```

### 自动断言（包）

```powershell
cd "D:\Jav1e Flies\00-工作区\Harness-core"
pytest -q                                         # 597 passed
python -m mypy harness_core --strict --ignore-missing-imports   # 0 issues
python -m pyflakes harness_core tests                           # 0 warnings
harness-core eval                                 # 10/10, exit 0
python artifacts/mutation_check.py                # 82/82 killed
python artifacts/audit_offline.py                 # 离线证明
```

### 人工项

- [ ] 详情页在 375px 宽度下无横向溢出
- [ ] 深色模式下正文对比度足够
- [ ] 减弱动效（`prefers-reduced-motion`）时页面可正常阅读
- [ ] GitHub 链接在新标签打开，且 `rel="noopener"`
- [ ] 页面无外链资源（唯一外部 URL 是 `canonical` 与 `og:image` 元数据）

---

## 五、和上次交付的关系

| | 上次（页面交付） | 这次（仓库 + 页面） |
|---|---|---|
| 产物 | 一个详情页 | 一个可安装的 Python 包 + 同一个详情页 |
| 数字来源 | 设计稿里的目标值 | 真实运行结果 |
| 可验证性 | 只能看页面 | `git clone` 后自己跑 |
| 技术深度证明 | 文案与架构图 | 597 测试 + 82 变异体 + 严格类型 |

**页面本身不用重做**。它是上一轮的成果，这轮只是让它背后的东西真实存在，并在页面上给出源码入口。

---

## 六、如果你要写进简历/自我介绍

一句话版本：

> 独立设计并实现 Harness-Agent-Core：位于 LLM 与业务系统之间的确定性驾驭层，
> 用工具白名单、确定性校验、自愈反馈、熔断与人工闸门把不确定的模型输出变成可验收交付。
> 零运行时依赖，597 个测试，82 个变异体全部被杀死。

技术要点（面试可展开）：

- **状态机归 harness，不归框架**：换 while / graph / LangGraph 三套驱动，判定逻辑一处不变
- **`step` 与 `retry_count` 分离**：成功调工具算进展，不算重试
- **拒绝先于审批**：白名单拦截发生在人工闸门**之前**，否则注入会变成钓鱼提示
- **`interrupt()` 的坑**：恢复时节点从头重跑，副作用必须能被状态判断跳过
- **脱敏不是拒绝**：拒绝会把密钥留给"换个说法"的重试，脱敏是把它从载荷里删掉

---

## 七、需要你决定的

1. **GitHub 链接放哪**（见第三节，推荐页首）
2. **sitemap / README / resume-data 三处**要不要一起改
3. 仓库要不要加 **CI 徽章实跑**（现在徽章是静态 SVG，接了 GitHub Actions 就能显示真实状态）

第 3 条如果你要，我可以加一个 workflow：push 时自动跑 `pytest` + `mypy` + `eval`，
失败就红。大约 20 行 YAML，`.github/workflows/ci.yml` 已经有草稿。
