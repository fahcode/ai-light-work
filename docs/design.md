# light-work 设计说明

## 分层

| 层 | 内容 | 谁来做 |
|---|---|---|
| 采集 | 用户意图 → 台账条目 | `lw-record` 技能（模型） |
| 约束 | 改代码前把相关约束交给 AI；改完逐条核对 | 钩子 + `lw.py context/score`（确定性），判断由模型做；明确冲突由 `lw-edit` 的冲突闸门交给用户选 |
| 呈现 | 按复杂度出 L0~L3 卡片 | `lw.py score` 定级别和素材，`lw-review` 写讲解 |
| 检查 | 条目之间的冲突、回归 | `lw.py conflicts/check` 找候选，`/lw-check` 判定 |
| 报告 | 台账范围 / 整个分支 | `/lw-report`、`/lw-branch` 写 Markdown，经 stdin 交给 `lw.py view` 渲染成 `.light-work/<分支>.html` |

原则：**能用规则算出来的放进脚本，结果可复现；模型只做蒸馏、判定和讲解。**

---

## 台账格式

每个分支一个文件：`.light-work/<分支>.md`（分支名里的 `/` 等字符换成 `-`，如 `feature/voice` → `feature-voice.md`）。当前在哪个分支就读写哪一份；会话中途切分支，下一次钩子触发时自动换过去，新分支第一次出现时自动建台账。

```markdown
### C-004 · 2026-11-19 · bug · open · 空结果被当成成功
- 意图: 用户："没说话也显示搜索成功" → 空识别结果返回失败
- 约束:
  - C-004.1 识别结果为空字符串时返回 ok:false reason=empty
  - C-004.2 网络错误仍然进入 error 态
- 假设: 前端用 reason 区分文案（未确认）
- 根因: 只判了 null，空字符串落到成功分支 voice.ts:2
- 文件: src/search/voice.ts
- 提交: a1b2c3d
- 验证: ✓ pnpm test voice → 16 passed ｜ ✗ 真机未跑
- 关联: supersedes C-003.1; relates C-001
- 过程: R1 判空；R2 用户要求网络错误保留 error 态
- 级别: L1
```

| 字段 | 含义 | 规则 |
|---|---|---|
| 标题行 | `ID · 日期 · 类型 · 状态 · 标题` | 类型：bug / change / refactor / chore；状态：open / settled / dropped |
| 意图 | 用户原话的关键词 → 一句话需求 | 不是 AI 的方案；以后"当时为什么这么定"就靠它 |
| 约束 | 改完之后应该成立的行为 | 必须能从代码判定；lint 会拦下"处理好边界"这类含糊的写法 |
| 假设 | AI 没得到确认就做的决定 | 会出现在卡片的"待你拍板"里 |
| 根因 | bug 的根本原因，带 `文件:行` | bug 定稿时必填 |
| 文件 | 改了哪些文件（也可以写目录，以 `/` 结尾） | 约束靠它和代码对上；记录时写预计的，改完按实际修正 |
| 提交 | 提交号 | post-commit 钩子自动回填，同时把已出过卡片的 open 条目转成 settled |
| 验证 | `✓ 做过的（命令 → 结果）｜ ✗ 没做的` | 不接受"应该没问题"这类说法 |
| 关联 | `supersedes X` 让旧约束失效；`relates Y` 只做关联 | 旧条目不改写 |
| 过程 | 同一意图的多轮修订 `R1 …；R2 …` | 第 3 轮时钩子会提醒先找根因 |
| 级别 | L0~L3，记录阶段写"待定" | 取各轮的最大值 |

约束只来自用户实际提出的修改，不会从已有代码里推断。（推断出来的约束不准，会带来误报和多余的提问。）

规则：open 条目可以继续改；提交后自动转 settled，之后只追加、不改写。用户不需要管理状态。

---

## 复杂度分级（`lw.py score`）

分级衡量的是**人要花多少力气才能看懂**，不是 AI 审得多深。所有信号都来自 git diff。

| 信号 | 级别 |
|---|---|
| 只改了测试、文档、锁文件或资源 | L0 |
| 有效改动 >10 行，或 >1 个文件 | L1 |
| ≥50 行 / >3 个文件 / 跨 2 个模块 / 删除导出或签名变化 / 改配置 / 关注词（cache、retry、timeout、order、fallback……）/ 敏感词零星出现 | L2 |
| ≥200 行 / >8 个文件 / ≥3 个模块 / 敏感词（auth、token、payment、refund、migration、transaction……）出现在路径里或至少 2 行 / schema、迁移、proto 文件 | L3 |

- 有效改动不算测试、锁文件、生成物、文档和图片
- 一个信号只按它能达到的最高级别计一次
- **触及台账约束不提高级别**：否则大多数改动都会变成 L2，人很快就看不下去了。但无论哪一级，卡片里都必须逐条核对约束
- 接口变化只算"删除导出"和"签名改变"；常量改值、新增导出不算

额外的 ⚠ 标记（不影响级别，卡片里必须回应）：
- 删除了某个符号，但 `git grep` 显示还有引用
- 有文件不属于任何台账条目（"改完这个带出那个"最常见的来源）
- 改了 ≥30 行逻辑，但没有任何测试改动
- 改动以删除为主

一层调用方（只展示，不影响级别、不加 ⚠）：签名没变、只改了内部逻辑的函数（最多 8 个），用 `git grep -w` 找本轮没改、也不是测试的文件里谁按名字引用了它，每个函数最多列 5 个文件。只做一层，同名会误报，由 lw-review 打开看一眼再决定要不要写进卡片。

## 范围

| 参数 | 范围 | 默认用于 |
|---|---|---|
| `--since-checkpoint` | 上次收尾点 → 工作区 | `score`（每次出卡片） |
| `--ledger` | 台账起点 → 工作区 | `check`、`/lw-report` |
| `--branch` | 与基准分支的分叉点 → 工作区 | `/lw-branch` |
| `--entry C-xxx` | 这个条目所有轮次的改动（可能不连续） | `/lw-report C-xxx` |
| `--base REF` | REF → 工作区 | 临时用 |

"工作区"包括未提交、未跟踪的文件（遵守 .gitignore）。脚本用一个临时 index 把工作区写成 git tree，不会动你的真实 index。

## 快照

| ref | 内容 |
|---|---|
| `refs/light-work/start/<slug>` | 第一次建台账时的工作区，即"台账范围"的起点 |
| `refs/light-work/cp/<slug>` | 收尾点链。每次 `checkpoint --entry C-xxx` 生成一个提交对象，记录 `entry=` 和 `from=`（本轮改动的起点） |

`/lw-check` 靠收尾点链拿到每个条目每一轮的 diff，再算出：
- **回退/改写**：后面条目删掉的行，正好是前面条目加的
- **热点**：同一个"文件 + 函数"被多个条目改过。函数的确定方法：改动行本身是定义就用它，否则在对应版本的文件里往上找最近的定义
- **反复修改**：≥3 轮的条目

这些 ref 不在任何分支上，不会被推送，也不出现在 PR 里。

## 钩子

| 事件 | 命令 | 行为 |
|---|---|---|
| SessionStart（startup / resume / clear / compact） | `hook session-start` | 把脚本复制到 `~/.light-work/lw.py`（给技能和 git 钩子一个稳定路径）；在需求分支上建台账、起点快照和 post-commit 钩子；注入工作流说明和约束摘要 |
| UserPromptSubmit | `hook prompt` | 列出 open 条目和轮次；像改代码的消息提醒先记录；第 3 轮提醒先找根因；清除"等待冲突确认"标记 |
| PreToolUse（Edit / Write / MultiEdit / NotebookEdit / Codex 的 apply_patch） | `hook pre-edit` | 文件路径取 `tool_input.file_path`；Codex 没有这个字段，从补丁文本的 `*** Add/Update/Delete File:`、`*** Move to:` 行解析，一个补丁可以改多个文件。注入这个文件相关的约束，每条只提示一次，并提醒"明确冲突先问"；没有 open 条目时提醒先记录。台账文件本身不触发 |
| PostToolUse（同上） | `hook post-edit` | 标记"本轮有改动" |
| Stop | `hook stop` | 本轮有改动，且工作区和上次收尾点不同时返回 `decision: block`，要求执行 lw-review。`stop_hook_active` 为真、或正在等用户回答冲突确认（`lw.py ask`）时放行；AI 最后一句以问号结尾时（Codex 读 `last_assistant_message`，Claude Code 读会话记录末尾）视为还在等用户，同一份改动只放过一次；同一份改动最多拦 2 次 |

Codex 和 Claude Code 的钩子协议基本一致（事件名、matcher、`additionalContext`、`decision: block`、`stop_hook_active`），插件钩子里 `CLAUDE_PLUGIN_ROOT` 在 Codex 下也有，所以三家共用一份 `hooks/hooks.json`。差别只有两处：Codex 编辑文件统一走 `apply_patch`；Codex 的钩子要用户在 `/hooks` 里信任后才运行。

钩子出任何异常都只写 stderr、以 0 退出，不会把会话弄坏。设 `LW_DEBUG=1` 可以看到完整报错。

状态文件：`.git/light-work/state-<slug>.json`（脚本自己维护，用户不用管）。

## 零配置

插件没有任何用户配置文件，也没有开关：

- **基准分支**：依次检测 `origin/HEAD` 指向的分支，以及 main、master、develop。在基准分支和 detached HEAD 上不记录
- **阈值和关键词**：内置在 `scripts/lw.py` 的 `DEFAULTS` 里。要调就直接改插件，所有仓库一起生效
- **测试命令**：AI 从项目自身（package.json 的 scripts、Makefile、CI 配置）推断
- **使用了 husky 等 `core.hooksPath` 的仓库**：跳过 post-commit 钩子，提交号会在下次收尾时补上

## 冲突闸门

`lw-edit` 动手前用改法对一遍约束，同时满足以下三条才问用户：

1. 改动会让某条有效约束**明确不成立**（能从代码判断）
2. 用户这次的话没有提到这个行为
3. 这条约束不是当前条目自己的 open 约束

问法固定：所有冲突合在一条消息里，每个冲突两三行，带编号选项，用户回一个数字就行。问之前调用 `lw.py ask`，Stop 钩子这一轮就不拦截；用户下一条消息到来时，标记自动清除。拿不准的情况不问，写进 `假设`，在卡片里列为"待你拍板"。

## 命令速查

```bash
L="python3 ~/.light-work/lw.py"
$L init                            # 不用手动跑，钩子会自动做
$L context --files src/a.ts       # 改 a.ts 之前要知道的约束
$L next-id
$L score [范围] [--hunks N] [--json]
$L checkpoint --entry C-004
$L ask                            # 冲突闸门提问前
$L why src/a.ts:42 [src/b.ts:10-20]   # 这行是哪条台账改动引入的
$L check [范围] [--json]
$L conflicts [--json]
$L lint | stats | backfill
$L view [--title T] [--open] < report.md   # 渲染成 .light-work/<分支>.html
```
