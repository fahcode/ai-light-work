# light-work 设计说明

## 分层

| 层 | 内容 | 谁来做 |
|---|---|---|
| 采集 | 用户意图 → 改动记录条目 | `lw-record` 技能（模型） |
| 约束 | 改代码前把相关约束交给 AI；改完逐条核对 | 钩子 + `lw.py context/score`（确定性），判断由模型做；明确冲突由 `lw-edit` 的冲突闸门交给用户选 |
| 呈现 | 按复杂度出 L0~L3 卡片 | `lw.py score` 定级别和素材，`lw-review` 写讲解 |
| 检查 | 条目之间的冲突、回归 | `lw.py conflicts/check` 找候选，`/lw-check` 判定 |
| 报告 | 审阅结论与代码证据 | `lw.py view` 接收结论和范围参数，生成带文件导航与 Git diff 的本地网页 |

原则：**能用规则算出来的放进脚本，结果可复现；模型只做蒸馏、判定和讲解。**

---

## 改动记录格式

每个分支一个文件：`.light-work/<分支>.md`（分支名里的 `/` 等字符换成 `-`，如 `feature/voice` → `feature-voice.md`）。当前在哪个分支就读写哪一份；会话中途切分支，下一次钩子触发时自动换过去，新分支第一次出现时自动建改动记录。

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
- **触及改动记录约束不提高级别**：否则大多数改动都会变成 L2，人很快就看不下去了。但无论哪一级，卡片里都必须逐条核对约束
- 接口变化只算"删除导出"和"签名改变"；常量改值、新增导出不算

额外的 ⚠ 标记（不影响级别，卡片里必须回应）：
- 删除了某个符号，但 `git grep` 显示还有引用
- 有文件不属于任何改动记录条目（"改完这个带出那个"最常见的来源）
- 改了 ≥30 行逻辑，但没有任何测试改动
- 改动以删除为主

一层调用方（只展示，不影响级别、不加 ⚠）：签名没变、只改了内部逻辑的函数（最多 8 个），用 `git grep -w` 找本轮没改、也不是测试的文件里谁按名字引用了它，每个函数最多列 5 个文件。只做一层，同名会误报，由 lw-review 打开看一眼再决定要不要写进卡片。

## 范围

| review / check 参数 | 范围 |
|---|---|
| 空 | 改动记录中最近一条 |
| `C-xxx` | 指定条目已收尾的各轮差异 |
| `all` / `a` | 所有改动记录条目的已收尾轮次 |
| `branch` / `b` | 与基准分支的分叉点 → 工作区 |
| `--base REF --current-entry C-xxx` | 编辑流程内部使用，范围是本轮起点 → 工作区 |

`lw.py review` 和 `lw.py check` 共用范围解析。review 输出人工审阅所需的改动、影响面和证据，分支模式生成合入报告。check 输出回归、冲突及验证缺口。手动调用缺少收尾点时提示差异不可用。底层 `score` 仍可用于查看上次收尾点后的差异。

"工作区"包括未提交、未跟踪的文件（遵守 .gitignore）。脚本用一个临时 index 把工作区写成 git tree，不会动你的真实 index。

改动记录的「验证」只保存实际执行的检查和结果。需要说明的验收缺口放在本轮报告。旧文件可以继续读取，未执行项目保留在历史原文里，摘要和报告概览只显示执行结果。

## 审阅网页

`view --scope latest|C-xxx|all|branch` 复用审阅的范围。自动审阅使用 `view --base REF --current-entry C-xxx`。Python 直接渲染范围、文件增删统计、代码证据和改动记录信息。助手通过标准输入补充解释和验证结论，反引号中的 `文件:行` 链接到对应补丁行或文件。多轮存在同一行号时优先使用最近的新版行，补丁标题保留快照范围。

生成产物、锁文件等被评分规则排除的文件只展示路径与统计。普通文件优先展示正文引用的差异，其余以有限的补丁片段作为补充。网页标明省略的片段和行数。完整差异通过 Git 查看。

正文支持标题、列表、表格和代码块。按场景对照改前和改后。共用数据、状态或条件的改动，说明相互影响并链接证据。L0、L1 以聊天短卡片为主。L2、L3 或分支报告可使用 `flow`、`sequence`、`architecture` 画流程、交互和模块关系，每行按 `来源 -> 目标 | 说明` 书写。简单 `flow` 也可每行写一个步骤。历史 Mermaid 块保留源文本。摘要、导航、补丁、图和改动记录均可离线阅读。省略范围参数时，`view` 仍可接收纯说明文字。

## 快照

| ref | 内容 |
|---|---|
| `refs/light-work/start/<slug>` | 第一次建改动记录时的工作区，即"改动记录范围"的起点 |
| `refs/light-work/cp/<slug>` | 收尾点链。自动 review 用 `checkpoint --entry C-xxx --base REF` 记录条目和本轮起点。起点 tree 由收尾点引用，供以后按条目审阅 |

`/lw-check` 靠收尾点链拿到每个条目每一轮的 diff，再算出：
- **回退/改写**：后面条目删掉的行，正好是前面条目加的
- **热点**：同一个"文件 + 函数"被多个条目改过。函数的确定方法：改动行本身是定义就用它，否则在对应版本的文件里往上找最近的定义
- **反复修改**：≥3 轮的条目

这些 ref 不在任何分支上，不会被推送，也不出现在 PR 里。

## 钩子

| 事件 | 命令 | 行为 |
|---|---|---|
| SessionStart（startup / resume / clear / compact） | `hook session-start` | 在 Git 工作区复制脚本到 `~/.light-work/lw.py`，建改动记录、起点快照和 post-commit 钩子，并注入工作流说明和约束摘要 |
| UserPromptSubmit | `hook prompt` | 列出 open 条目和轮次。命中改动关键词时提示 AI 判断用户是否要求动手，纯提问直接回答。第 3 轮提醒先找根因，并清除"等待冲突确认"标记 |
| PreToolUse（Edit / Write / MultiEdit / NotebookEdit / Codex 的 apply_patch） | `hook pre-edit` | 文件路径取 `tool_input.file_path`；Codex 没有这个字段，从补丁文本的 `*** Add/Update/Delete File:`、`*** Move to:` 行解析，一个补丁可以改多个文件。注入这个文件相关的约束，每条只提示一次，并提醒"明确冲突先问"；没有 open 条目时提醒先记录。改动记录文件本身不触发 |
| PostToolUse（同上） | `hook post-edit` | 标记"本轮有改动" |
| Stop | `hook stop` | 有改动尚未收尾时，通过 `systemMessage` 提醒可以补做 lw-review，会话正常结束。同一份改动只提示一次，等待冲突确认时跳过 |

Codex 和 Claude Code 的钩子协议基本一致（事件名、matcher、`additionalContext`、`systemMessage`、`stop_hook_active`），插件钩子里 `CLAUDE_PLUGIN_ROOT` 在 Codex 下也有，所以三家共用一份 `hooks/hooks.json`。差别只有两处：Codex 编辑文件统一走 `apply_patch`；Codex 的钩子要用户在 `/hooks` 里信任后才运行。

钩子出任何异常都写 stderr、以 0 退出，不会把会话弄坏；同时记到 `~/.light-work/hook-error.txt`（脚本复制失败、post-commit 安装失败也记），下一次 SessionStart 或 UserPromptSubmit 把它注入给 AI，让 AI 告诉用户一次，然后删掉。路径一律先取 `realpath` 再算仓库相对路径（macOS 的 `/tmp`、`/var` 是符号链接）。设 `LW_DEBUG=1` 可以看到完整报错。

状态文件：`.git/light-work/state-<slug>.json`（脚本自己维护，用户不用管）。

## 零配置

插件没有任何用户配置文件，也没有开关：

- **Git 工作区**：基准分支同样启用自动流程，detached HEAD 使用 `detached-<当前提交短 SHA>` 标识。基准分支仍用于计算分支差异范围
- **阈值和关键词**：内置在 `scripts/lw.py` 的 `DEFAULTS` 里。要调就直接改插件，所有仓库一起生效
- **测试命令**：AI 从项目自身（package.json 的 scripts、Makefile、CI 配置）推断
- **使用了 husky 等 `core.hooksPath` 的仓库**：跳过 post-commit 钩子，提交号会在下次收尾时补上

## 冲突闸门

`lw-edit` 动手前用改法对一遍约束，同时满足以下三条才问用户：

1. 改动会让某条有效约束**明确不成立**（能从代码判断）
2. 用户这次的话没有提到这个行为
3. 这条约束不是当前条目自己的 open 约束

问法固定：所有冲突合在一条消息里，每个冲突两三行，带编号选项，用户回一个数字就行。问之前调用 `lw.py ask`，Stop 钩子这一轮就跳过提醒；用户下一条消息到来时，标记自动清除。拿不准的情况不问，写进 `假设`，在卡片里列为"待你拍板"。

## 命令速查

```bash
L="python3 ~/.light-work/lw.py"
$L init                            # 不用手动跑，钩子会自动做
$L context --files src/a.ts       # 改 a.ts 之前要知道的约束
$L next-id
$L review [C-xxx | all | branch] [--hunks N] [--json]
$L review --base <tree SHA> --current-entry C-004
$L snapshot                        # 编辑前获取本轮起点 tree SHA
$L checkpoint --entry C-004 --base <tree SHA>
$L ask                            # 冲突闸门提问前
$L why src/a.ts:42 [src/b.ts:10-20]   # 这行是哪条改动记录引入的
$L check [C-xxx | all | branch] [--json]
$L check --base <tree SHA> --current-entry C-004
$L conflicts [--json]
$L lint | stats | backfill
$L view --scope branch [--title T] [--open] < report.md   # 渲染成 .light-work/<分支>.html
```
