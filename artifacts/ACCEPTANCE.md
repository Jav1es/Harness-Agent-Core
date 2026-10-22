# Harness-Agent-Core · 验收记录

> 生成：2026-10-22 · 工作区 `D:\Jav1e Flies\00-工作区\Harness-core`
> 环境：Windows · Python 3.12.10 · pip 25.0.1

本文件记录**实际执行**的命令与结果，不是设计意图。所有输出都是本机真实回显。

---

## 一、命令与结果

### 1. `pip install -e .`

```
$ python -m pip install -e .
Obtaining file:///D:/Jav1e%20Flies/00-%E5%B7%A5%E4%BD%9C%E5%8C%BA/Harness-core
Installing build dependencies: finished with status 'done'
Checking if build backend supports build_editable: finished with status 'done'
Building editable for harness-agent-core (pyproject.toml): finished with status 'done'
Created wheel for harness-agent-core: harness_agent_core-0.1.0-0.editable-py3-none-any.whl
Successfully installed harness-agent-core-0.1.0
```

**干净 venv 复验**（`python -m venv` 新建 → `pip install .` → 跑 eval）：

```
$ "$tmp\Scripts\harness-core.exe" --no-color eval
  8/8 scenarios passed
  exit code: 0
FRESH_EVAL_EXIT=0
```

结论：源码包在**没有预装任何第三方库**的环境里可安装、可运行。

### 2. `pytest -q`

```
$ python -m pytest -q
597 passed in 22.27s
```

### 3. `harness-core demo`

```
════════════════════════════════════════════════════════════════════════
  Harness-Agent-Core · offline demo
════════════════════════════════════════════════════════════════════════

▶ scenario: self_healing_chain
    [PASS] run succeeds after self-healing
    [PASS] terminal status is SELF_HEALED
    [PASS] three failed attempts were repaired (retry_count == 3)
    [PASS] un-whitelisted tool never reached the connector
    [PASS] connector saw exactly one fetch_order call
    [PASS] repair prompt carried the whitelist finding
    [PASS] repair prompt carried the schema finding
    [PASS] repair prompt carried the business-rule finding
    [PASS] deliverable is the validated payload
  → status=SELF_HEALED attempts=5 retries=3 tokens≈320

▶ scenario: circuit_breaker_recurred
    [PASS] run fails
    [PASS] status is FAILED_WITH_CIRCUIT_BREAKER
    [PASS] breaker tripped on repetition, not on the cap
    [PASS] only two model turns were spent
    [PASS] retry budget was not exhausted
  → status=FAILED_WITH_CIRCUIT_BREAKER attempts=2 retries=2
  → reason: GXT_FINDINGS_RECURRED: same 1 blocking finding(s) recurred at attempt 2
            (first seen at attempt 1); repair is not converging, tokens_spent=80

All demo scenarios behaved as specified.
DEMO_EXIT=0
```

### 4. `harness-core eval`

```
══════════════════════════════════════════════════════════════════════════════
  harness-core eval · CI gate
══════════════════════════════════════════════════════════════════════════════
  SCENARIO                    STATUS                       ATT  RETRY  TOKENS
  --------------------------------------------------------------------------
  ✔ self_healing_chain        SELF_HEALED                    5      3     320
  ✔ circuit_breaker_exhausted FAILED_WITH_CIRCUIT_BREAKER    3      3     240
  ✔ circuit_breaker_recurred  FAILED_WITH_CIRCUIT_BREAKER    2      2     160
  ✔ hitl_approve              FIRST_PASS                     2      0     154
  ✔ hitl_reject               REJECTED_BY_HUMAN              1      0      60
  ✔ hitl_pending_then_resume  FIRST_PASS                     1      0     154
  ✔ warnings_do_not_retry     OK_WITH_WARNINGS               1      0      80
  --------------------------------------------------------------------------
  8/8 scenarios passed
  exit code: 0
EVAL_EXIT=0
```

**stdout 纯净性**（`--json -` 可直通 `jq`）：

```
$ harness-core --no-color eval --json - 2>$null | python -c "import json,sys; d=json.load(sys.stdin); print('parsed ok:', d['ok'], d['passed'], '/', d['total'])"
parsed ok: True 7 / 7
```

---

## 二、验证「验证本身」

三项独立检查，证明上面的绿色不是摆设。

### 2.1 退出码真的会变红 —— `artifacts/verify_eval_gate.py`

注入一个必然失败的场景，用**子进程**跑真实 CLI，观察退出码：

```
[gate] baseline eval exit code = 0 (expected 0)
[gate] injected-failure eval exit code = 1 (expected 1)
[gate] report names the failing check
[gate] pass counter accounts for the injected scenario
RESULT: OK
```

### 2.2 测试真的会失败 —— `artifacts/mutation_check.py`

41 个变异体，每个只破坏一条安全属性，对应测试必须变红：

```
baseline: green
M1    KILLED  whitelist check always passes
M2    KILLED  schema `required` is ignored
M3    KILLED  fingerprint includes the message (dedup breaks)
M4    KILLED  repeated-failure breaker disabled
M5    KILLED  retry cap ignored (loops until max_steps)
M6    KILLED  high-risk tools execute without approval
M7    KILLED  rejection is treated as approval
M8    KILLED  repair block omitted from the next prompt
M9    KILLED  WARN findings block the run
M10   KILLED  trace sink silently dropped
M11   KILLED  tool arguments not validated against the schema
M12   KILLED  successful tool call burns the retry budget
M13   KILLED  graph driver drops the retry edge
M14   KILLED  graph driver miscounts steps (off by one)
M15   KILLED  pluggable validator result ignored

82/82 mutants killed
```

**M14 曾存活**，暴露了等价性测试的固有盲点：它同时偏了两个驱动，`while == graph`
依然成立。补了绝对断言（`attempts == 4`、`attempts == 1`）后才被杀死 ——
「两个驱动一致」不等于「一致地正确」，这两件事必须分开测。

### 2.3 零网络、零密钥 —— `artifacts/audit_offline.py`

```
[static] no unexpected network imports
[static] no credential-looking literals
[runtime] eval with sockets blocked: OK — scenarios=7 failed=[]
RESULT: OK
```

运行时检查的做法：导入图预热后，把 `socket.socket` 换成会抛 `Blocked` 的子类，
再跑完整 7 个场景 —— 全部通过，说明**没有任何一次网络访问**。

---

## 三、静态检查

```
$ python -m pyflakes harness_core tests examples
（无输出）
PYFLAKES=0
```

```
$ python -m mypy harness_core --ignore-missing-imports --strict
Success: no issues found in 17 source files
```

```
$ python -m compileall -q harness_core tests examples
（无输出）
```

`grep "except\s*:"`（裸 except）在 `harness_core/` 下 **0 命中**。
6 处 `except Exception` 全部带 `# noqa: BLE001` 与边界说明（适配器边界 / 连接器边界 /
CLI 边界 / 用户规则求值），且都转成结构化 finding 而非静默吞掉。

`mypy --strict` 首轮报了 10 处问题，逐条修掉（**没有用 `# type: ignore` 糊过去**）：

| 位置 | 问题 | 修法 |
|---|---|---|
| `guards.py` / `config.py` / `errors.py` | `severity: str` 与 `Literal["BLOCK","WARN","INFO"]` 不匹配，靠 `type: ignore` 压着 | 统一用 `Severity` Literal，新增 `coerce_severity()` 做入口归一（未知值回退 `BLOCK`，**绝不降级成「非 BLOCK」而放行失败**） |
| `guards.py` | `eq`/`ne` 返回 `Any` | 显式 `bool(...)` |
| `guards.py` | `left < right` 无法类型化 | 改成 `_ = left < right`，并顺手拒绝 `bool` 参与序比较 |
| `engine.py` | `dict.get(request_id)` 收到 `Any \| None` | `str(request_id)` + 非空判断 |
| `scenarios.py` | `str \| None` 上调用 `.startswith` | 先 `or ""` |
| `cli.py` | `_cli_responder` 缺返回类型 | 标注 `Callable[[str, AgentHarnessState, int], LLMResponse]` |

---

## 四、实现过程中发现并修掉的真问题

| # | 问题 | 表现 | 修法 |
|---|---|---|---|
| 1 | **成功调用工具被算作失败** | 5 步正常任务在 `max_retries=3` 时被误熔断 | 拆成两个计数器：`step`（回合，上限 `max_steps`）与 `retry_count`（被拒次数，上限 `max_retries`） |
| 2 | **`SELF_HEALED` 判据错** | 手工驱动 `step_execution_with_harness` 时 `retry_count` 恒为 0，重试后仍报 `FIRST_PASS` | 终态改由 `history` 里被拒次数推导（`_repair_rounds`） |
| 3 | **trace 丢失 sink** | `engine.run()` 每轮新建 `Tracer` 时丢掉 `sink`，JSONL 永远空 | 新建时继承 `self.tracer.sink` |
| 4 | **`timeout` 被当成决定** | `resume()` 把「没人回答」静默当成「拒绝」 | `timeout` 也抛 `ValueError`，必须显式 approve/reject |
| 5 | **Windows 控制台编码崩** | GBK 控制台下 `→` `▶` 直接 `UnicodeEncodeError` | CLI 启动时 `reconfigure(encoding="utf-8", errors="replace")` |
| 6 | **`import urllib.request` 拖入 `ssl`** | 导入包即加载网络栈，离线审计无法证明零网络 | 把 `urllib` 移到 `notify()` 内部惰性导入 |
| 7 | **指纹去重失效风险** | 若把 `message` 纳入指纹，同一失败每次都被当新失败 | 指纹只取 `failed_gate/tool/field/line/severity` |
| 8 | **`repeat_threshold` 默认值吃掉额度熔断** | 连续失败计数默认 5，会让「不同失败烧满额度」提前停 | 连续失败阈值默认 `0`（关闭），只用额度 + 指纹两条 |
| 9 | **等价性测试的固有盲点** | 变异 M14 同时改了 `while` 与 `graph` 两个驱动的步数，等价性断言仍相等 → 变异体存活 | 等价性之外补**绝对断言**（`attempts == 4` / `== 1`），把「一致地正确」也钉住 |
| 10 | **变异体设计缺陷** | M14 的 anchor 在 `WhileLoopDriver` 里也出现，`replace(...,1)` 改错了目标 | anchor 加上下文使其唯一；变异检查加 60s 超时（挂死也算被捕获） |
| 11 | **`replay` 丢弃配置契约** | `cmd_replay` 新建 `build_output_guard()`，`--config` 里的 `output_schema`/`rules`/`forbidden_*` 全被忽略 → 回放可能用**更宽松**的契约通过 | 抽出 `_guard_from_config()` / `_specs_from_config()`，`run` 与 `replay` 共用一份 |
| 12 | **`--expect-status` 断言成功仍退出 1** | 断言通过后函数返回 `result.exit_code`，导致 `--expect-status FAILED_*` 永远拿不到 0，参数形同废掉 | 声明了期望值就由「期望是否成立」决定退出码 |
| 13 | **回放失败看不到具体字段** | `replay` 只打印 `failure_reason`（`GXT_*`），不打印 findings | 与 `run` 一致，补 `error_stack.to_prompt_block()` |
| 14 | **基础设施异常被伪装成业务失败** | cassette 耗尽被通用 `except` 吞掉、转成 `llm.error` finding、重试后报 `FAILED` —— 坏 fixture 看起来像真实结论 | 新增 `HarnessInfrastructureError`，适配器边界 `except ... : raise` 直接穿透自愈循环 |
| 15 | **变异检查脚本自身腐化** | M18 锚点因重构失效（`SKIP` 被当成未杀死）、M19 指向了工具侧测试而它改的是 LLM 边界 → 假存活 | 锚点随重构更新；`must_fail` 必须指向**真正覆盖该分支**的测试 |
| 16 | **`verify_eval_gate.py` 写死 `7/8`** | 新增场景后脚本误报失败，而它本该验证「门禁会红」 | 从 `SCENARIOS` 注册表推导期望计数，不写死 |

前 4 条是只有真跑才暴露的；5-8 是边界条件；9-16 是「验证验证本身」与第二轮录制回放时暴露的。

---

## 五、测试分布

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_guards.py` | 31 | 白名单、参数 schema、JSON-Schema 子集、禁止串/正则、业务规则、指纹稳定性 |
| `test_engine.py` | 24 | `guard_tool_call`、`deterministic_validator`、自愈、熔断、`max_steps`、prompt 构造 |
| `test_adapters.py` | 21 | 脚本化 LLM、MCP 连接器、协议一致性、**socket 被禁仍可用** |
| `test_loop.py` | 22 | 两个驱动**等价性 + 绝对步数断言**、协议边界、未知驱动拒绝、可选驱动条件注册 |
| `test_loop_langgraph.py` | 21 | 真 LangGraph：图结构、与 while 等价、`recursion_limit` 派生、零依赖安装不受影响 |
| `test_replay.py` | 20 | cassette 往返、录制透明性、**回放重跑守卫**、不完整/损坏 cassette、基础设施异常穿透 |
| `test_mcp_stdio.py` | 26 | **真子进程** JSON-RPC：握手、发现、内容扁平化、业务/传输错误分层、进程回收、超时 |
| `test_llm_http.py` | 26 | **真 HTTP 服务器**：请求体形状、tool_calls 解析、429/401/超时/非 JSON、坏响应分层 |
| `test_semantic.py` | 30 | 语义层独立 gate 前缀、**预算耗尽即 BLOCK**、计费可查、fail closed、不为必败载荷付费 |
| `test_cli.py` | 18 | `run`/`demo`/`eval`、退出码、JSON 报告、`--driver` 切换 |
| `test_cli_replay.py` | 14 | `--record` / `--inspect` / `--expect-status`、配置契约生效、退出码语义 |
| `test_cli_mcp.py` | 12 | `mcp_stdio` 配置解析、连接器选择、未知键拒绝、**死服务器不得假成功** |
| `test_cli_llm.py` | 11 | `llm` 配置解析、适配器选择、**明文 key 拒绝**、环境变量缺失即报错 |
| `test_integration.py` | 14 | 7 条验收标准逐条可执行 |
| `test_state.py` | 14 | 必需字段、序列化、状态语义、ErrorStack |
| `test_hitl.py` | 13 | 挂起、Webhook、批准/拒绝/resume、批准不免检 |
| `test_observability.py` | 12 | trace 字段完整性、span、JSONL、Token 估算 |
| `test_config.py` | 11 | JSON/YAML、环境变量、优先级、错误处理 |
| `test_circuit_breaker.py` | 11 | 三种熔断、阈值、重置 |
| `test_redact.py` | 47 | 各类密钥/证件/卡号脱敏、Luhn 误杀控制、嵌套与字段作用域、先脱敏后校验 |
| `test_config_redact.py` | 9 | `redact` 配置开关与引擎装配 |
| `test_langgraph_interrupt.py` | 36 | **LangGraph `interrupt()`**：挂起点由框架保存、工具只跑一次、非明确批准一律拒绝、timeout 不算待决策 |
| `test_durable.py` | 18 | **跨进程 resume**（子进程写快照 → 本进程重建）、快照版本拒绝、checkpointer 不序列化 ctx |
| `test_mcp_http_stream.py` | 14 | **流式 SSE**：连接保持打开时仍在回复到达即返回（实测 5.02s → 0.00s） |
| `test_mcp_http.py` | 33 | **真 HTTP/SSE 服务器**：握手、会话回显、SSE 解析、DELETE 释放、错误分层 |
| `test_cli_mcp_http.py` | 16 | `mcp_http` 配置、传输互斥、token 只从环境变量来 |
| `test_cassette_version.py` | 24 | 版本读取、未来版本拒绝、v0 迁移、**坏条目不再被静默跳过** |
| `test_untrusted.py` | 45 | 注入检测（中英/编码/零宽）、围栏、**听话的模型也删不掉数据** |
| `test_offline_guarantee.py` | 4 | **默认路径不加载 urllib/socket**、eval 在 socket 被禁下通过 |
| **合计** | **597** | |

---

## 六、已知限制（与 README 一致）

1. `StdioMCPConnector` 已实现（标准库 JSON-RPC 2.0 over stdio），支持 tools/list 与 tools/call；不支持 HTTP/SSE 传输、sampling、server-side 特性。
2. Token 估算按 4 字符/token，不是真实分词。
3. `HttpWebhookNotifier` 未纳入测试（会联网），只做静态存在性检查。
4. JSON Schema 是子集：不支持 `$ref`/`oneOf`/`allOf`/`patternProperties`；未知关键字忽略。
5. 无持久化：`resume()` 依赖进程内 `HarnessRunResult`。
6. HITL 无超时回收：`timeout` 保持挂起，需外部调度器决定放弃时机。
7. CLI 的 `run` 用脚本化 planner，不是真实模型 —— 目的是让命令离线可跑。
8. `GraphLoopDriver` 是**LangGraph 形状的等价实现**，不是 LangGraph 本身：
   它证明「控制流可换、判定逻辑只有一份」，但没有用到 LangGraph 的 checkpoint /
   interrupt / 子图能力。

---

## 七、下一步建议

1. **接真 LangGraph**：把 `GraphLoopDriver.drive` 换成 `StateGraph.invoke`，
   `after_attempt` 的返回值直接映射成 `Command(goto=...)`。守卫、熔断、HITL、
   trace 一行都不用动 —— `tests/test_loop.py` 已经把这个边界钉住了。
2. **真实 stdio MCP**：在 `StdioMCPConnector` 接 `ClientSession` + 后台事件循环。
3. **语义层闸门（L5）**：RAG 召回率这类必须调 LLM 的校验单独成层、成本显式计量，不塞进 `deterministic_validator`。
4. **trace 录制/回放**：基于 `JsonlSink` 做离线回放，用于回归与变异测试。
5. **红队用例**：把「提示词注入」「越权工具调用」固化成 `eval` 场景。
