# 更新记录

## light-work 0.6.3 · 2026-11-20

- lw-review 和 lw-check 共用范围参数。无参数取最近一条，也可指定条目、all（a）或 branch（b）。编辑流程内部调用聚焦本轮。
- lw-review 负责审阅改动和合入报告，lw-check 聚焦回归、冲突及验证缺口。移除独立的 lw-report 和 lw-branch。
- 命令列表的描述先显示用途。

## light-work 0.6.2 · 2026-11-20

- 命中改动关键词时，由 AI 结合上下文判断用户是否要求动手。纯提问直接回答。
- Git 工作区统一启用自动流程，包含基准分支和 detached HEAD。
- Stop 在漏收尾时只提醒一次，允许会话正常结束。日常改完仍由 lw-review 自检并记录收尾点。

## light-work 0.6.1 · 2026-11-20

修复：
- 路径里有符号链接时（macOS 的 `/tmp`、`/var`，或通过链接打开项目），编辑钩子整个失效：约束不注入、改完不拦。现在两边都先取真实路径
- 删除整个文件时 diff 内容丢失，删除导出、引用检查、关键片段都看不见
- 只改锁文件（如 `pnpm-lock.yaml`）被判成 L2"改了配置"。配置和 schema 只看有效文件
- 钩子出错不再完全静默：记下来，下次会话开始或用户发消息时让 AI 告诉用户一次
- `~/.light-work/lw.py` 改成原子替换，Claude 和 Codex 里装了不同版本时不会读到半个文件

## light-work 0.6.0 · 2026-11-20

- 支持 Codex：新增 `.codex-plugin/plugin.json` 和 `.agents/plugins/marketplace.json`；编辑钩子能解析 Codex `apply_patch` 补丁里的文件路径（之前在 Codex 下会静默失效：约束不注入、改完不拦）
- 手动命令 `lw-check` / `lw-report` / `lw-branch` 在 Codex 里也不会自动触发（`agents/openai.yaml` 里 `allow_implicit_invocation: false`）；参数写法兼容不替换 `$ARGUMENTS` 的工具
- 新增 `lw.py why 文件:行`：git blame → 提交 → 台账条目，带出当时的意图、约束、根因，以及后来有没有被推翻；`lw-edit` 修 bug 前会用
- Stop 少误拦：AI 最后一句是在问用户时，同一份改动先放过一次
- `score` 新增"一层调用方"：改了内部逻辑的函数，列出本轮没改但按名字引用它的文件，只展示不抬级别

## light-work 0.5.0 · 2026-11-20

对照需求逐项审查，去掉没有对应需求的设计，补上一处缺口：

- 去掉"首次记录时自动草拟 B-000 基线"：从代码推断出来的约束不准，会带来误报和多余的冲突提问；约束只来自用户实际提出的修改
- 条目字段精简：「来源」合并进「意图」，「定稿」改名为「文件」（旧字段名仍然兼容）
- 补缺口：L2、L3 卡片带流程图，聊天窗口不一定能显示，现在同时渲染到 `.light-work/<分支>.html`
- `lw-review` 去掉 L3 占比统计提示
- README 新增「定位」和「需求和功能对照」

## light-work 0.4.2 · 2026-11-20

- 存储改为扁平结构：每个分支只有一个文件 `.light-work/<分支>.md`，不再建子目录
- review 卡片只在聊天里输出，不另存文件
- `/lw-report`、`/lw-branch` 的报告经 stdin 直接渲染成 `.light-work/<分支>.html`，不留中间文件

## light-work 0.4.1 · 2026-11-20

- `lw-edit` 精简到"像靠谱的同事那样改"：只改该改的、改一处不坏别处、不过度、写清楚为什么；明显冲突先问一句（从 98 行缩到 45 行）
- 会话开始注入的"当前分支状态"明确为自动生成、不会过期的 handoff
- README 重写：补全设计目的、边界、全部功能，以及台账和 handoff 的区别

## light-work 0.4.0 · 2026-11-20

- 统一命名：插件改名为 `light-work`；自动技能 `lw-record` / `lw-edit` / `lw-review`；命令 `/lw-check` / `/lw-report` / `/lw-branch`；脚本 `lw.py`
- 零配置：去掉 config.json 和暂停开关。台账按分支自动建、自动切换，存放在 `.light-work/<分支>/`
- 会话中途切换分支也会自动换成对应分支的台账
- `git commit` 后，已出过卡片的条目自动转为 settled，用户不需要管理状态
- `lw-edit` 新增冲突闸门：改动明显违反已有约束而用户没提到时，用一句话、编号选项请用户确认；等待确认时 Stop 钩子不拦截
- 冒烟测试扩展到 36 项（新增冲突闸门、自动定稿、分支切换）

## 0.3.0 · 2026-11-20（当时叫 branch-ledger）

- 仓库初始化为 ai-light-work 插件市场
- surgical-edit 按 Karpathy 风格编码准则的四条原则重写；bug 类修改改为先找根因（参考 obra/superpowers 的 systematic-debugging）
- 台账新增「根因」字段：已定稿的 bug 没写根因时 lint 报错
- 验证必须是证据：「应该没问题」这类表述会被 lint 拒绝（参考 superpowers 的 verification-before-completion）
- 同一条目改到第 3 轮时，钩子提醒停下来重新找根因
- 新增 tests/smoke.sh 端到端冒烟测试（27 项）

## 0.2.0

- 改成插件：3 个自动技能（ledger-record / surgical-edit / ledger-closeout）、3 个手动命令（/ledger-check、/ledger-report、/branch-report）、5 个钩子
- 每轮收尾点绑定条目编号，/ledger-check 能找出条目之间的回退和热点
- 拆分「台账改动报告」和「整个分支汇总」

## 0.1.0

- 台账 + 确定性复杂度打分（L0-L3）+ 影响面（git grep）+ 未登记改动检测
