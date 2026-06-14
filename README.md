# perpetual-repair

一个长驻脚本，驱动 [`claude`](https://docs.anthropic.com/en/docs/claude-code) headless CLI，对一个 git 仓库按闭环不停运转，自动发现并修复问题、开 PR、合并。

> **审查 → 并行修复 → 集成 → 校验+维修 → 开 PR 并合并 → 更新本地 base → 循环**

适用于任何语言的仓库——校验命令可配（默认 Go，可换成任意 `build`/`test` 命令）。

## 闭环流程

```
┌─────────────────────────────────────────────────────────────┐
│  审查(audit)  ──► 并行修复(fix) ──► 集成(integrate)            │
│   1 个 agent       N 个 agent          合并各修复分支          │
│   通读仓库         各占一个 worktree    到 1 个集成分支         │
│                                            │                  │
│                                            ▼                  │
│   下一轮 ◄── 清理 ◄── 开 PR 并合并 ◄── 校验+维修(verify)      │
│             worktree   gh pr + 更新 base   1 个 agent 跑       │
│                                            build/test 并修回归 │
└─────────────────────────────────────────────────────────────┘
```

- **审查**：一个 agent 通读整个仓库，产出结构化 findings（写入 `.perpetual-repair/findings-*.json`）。
- **并行修复**：每个 finding 交给一个独立 agent，各自在隔离的 git worktree + 独立分支里修，互不覆盖。并发数由 `--max-workers` 控制（默认 3）。
- **集成**：把各修复分支 `merge --no-ff` 到一个集成分支；遇冲突的修复自动跳过、留到下一轮，不让一个冲突毁掉整轮。
- **校验+维修**：一个 agent 在集成分支上跑 build / test，修掉合并引入的回归，直到绿；脚本侧再独立复核一次 build。
- **落地**：`gh pr create` + `gh pr merge --squash`，然后把本地 base 分支快进到远端（不切换、不碰你当前工作区）。
- **永续**：循环往复；连续若干轮无新发现则进入长休眠，之后再次审查。`Ctrl-C` 在当前轮结束后优雅退出（再按一次强制退出）。

## 依赖

- [`claude`](https://docs.anthropic.com/en/docs/claude-code)（Claude Code CLI，headless `-p` 模式）
- `git`、`python3`（仅标准库）
- 落地（开 PR/合并）时还需 [`gh`](https://cli.github.com/)（已登录）
- 校验阶段默认用 Go 命令，可用 `--build-cmd` / `--test-cmd` 换成任意语言

## 用法

```bash
# 永续模式：审查→并行修复→校验→自动开 PR 并 merge，循环不停
python3 perpetual_repair.py --repo /path/to/your/repo

# 只跑一轮
python3 perpetual_repair.py --repo /path/to/repo --max-rounds 1

# 只审查并打印计划，不动代码（最安全，先看它能发现什么）
python3 perpetual_repair.py --repo /path/to/repo --dry-run --max-rounds 1

# 修 + 校验，但不开 PR、不合并（集成分支留在本地给你 review）
python3 perpetual_repair.py --repo /path/to/repo --no-land

# 非 Go 项目：换校验命令
python3 perpetual_repair.py --repo /path/to/repo \
  --build-cmd "npm run build" --test-cmd "npm test"
```

后台长驻：

```bash
nohup python3 perpetual_repair.py --repo /path/to/repo > repair.log 2>&1 &
echo $! > repair.pid          # 记 PID
tail -f repair.log            # 看进度
kill -INT "$(cat repair.pid)" # 优雅停止（跑完当前轮）
```

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | 当前目录 | 仓库根目录 |
| `--base-branch` | `main` | 基线/合并目标分支 |
| `--remote` | `origin` | 远端名 |
| `--max-workers` | `3` | 并行修复 agent 上限（控内存） |
| `--max-findings` | `5` | 每轮最多处理几个问题（控 PR 体量） |
| `--max-rounds` | `0` | 0 = 无限 |
| `--idle-seconds` | `300` | 一轮无发现后的休眠 |
| `--build-cmd` / `--test-cmd` | `go build ./...` / `go test ./...` | 校验命令 |
| `--audit-model` / `--fix-model` / `--verify-model` | 空(用默认) | 各阶段模型别名，如 `sonnet` |
| `--no-land` | 关 | 不开 PR、不合并 |
| `--no-push` | 关 | 不推送远端（纯本地集成） |
| `--dry-run` | 关 | 只审查打印计划 |
| `--safe-permission` | 关 | 用 `acceptEdits` 而非跳过全部权限确认 |

## 设计要点

- **隔离优先**：每个修复 agent 一个 worktree + 一个分支，并行不互相覆盖。
- **不破坏你的工作区**：所有 git 操作都在临时 worktree 里做；更新本地 base 用 `git fetch origin <base>:<base>` 快进，不 checkout、不动你当前分支的未提交改动。worktree 元数据操作加全局锁，避免并发争 `index.lock`。
- **失败隔离**：单个 agent 异常 / 单个修复冲突 / 单轮校验不过，都只影响当轮当项，不会让循环停摆。
- **可观测**：心跳线程每 30 秒打一次当前阶段进度。
- **运行态产物**：findings、PR body 等写在 `.perpetual-repair/`（已加入 `.gitignore`）。

## 权限说明

默认用 `--dangerously-skip-permissions` 让各 agent 全自动执行（改文件、跑构建、git、gh）。
如需更保守，加 `--safe-permission` 改用 `acceptEdits`（仅自动接受编辑，其余仍可能停在确认）。
请在你信任的仓库与环境中运行——它会自动改代码、提交并合并到远端。

## License

MIT
