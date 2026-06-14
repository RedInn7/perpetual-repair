#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
perpetual_repair.py — 永续自修复编排器

一个长驻脚本，驱动 `claude` headless CLI，按如下闭环不停运转：

    审查(audit) → 并行修复(fix) → 集成(integrate) → 校验+维修(verify/repair)
        → 开 PR 并合并(land) → 更新本地 main → 清理 → 下一轮

每一类角色都是一个独立的 claude headless 进程，互相之间用 git worktree 做隔离，
保证多个修复 agent 并行时不会互相覆盖代码（符合仓库的 Worktree 开发规范）。

只依赖标准库 + 外部命令：claude / git / gh / go(可选)。

用法：
    python3 scripts/perpetual_repair.py                 # 永续模式，自动开 PR 并 merge
    python3 scripts/perpetual_repair.py --max-rounds 1  # 只跑一轮
    python3 scripts/perpetual_repair.py --no-land       # 修+校验但不开 PR、不合并
    python3 scripts/perpetual_repair.py --dry-run       # 只审查、打印计划，不动代码

按 Ctrl-C 可在当前轮次结束后优雅退出。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 全局配置
# --------------------------------------------------------------------------- #

LOG = logging.getLogger("perpetual")

# 在轮次中途收到 SIGINT 时置位，让循环跑完当前轮后停下，而不是粗暴中断
_STOP = threading.Event()

# git worktree / branch / prune 这些操作会改 .git 元数据与 index.lock，
# 多个并行修复线程同时跑会撞锁。所有改动 worktree 的 git 操作走这把锁串行化。
_GIT_WT_LOCK = threading.Lock()


@dataclass
class Config:
    repo_root: Path
    base_branch: str = "main"
    remote: str = "origin"
    # 模型：留空则用 claude 默认；可用别名如 "sonnet" / "opus" 控成本
    audit_model: str = ""
    fix_model: str = ""
    verify_model: str = ""
    # 并行修复 worker 上限——控内存，别把机器打爆
    max_workers: int = 3
    # 每轮最多处理多少个发现，控制每个 PR 的体量
    max_findings_per_round: int = 5
    # claude 单次调用的 turn / 超时上限
    audit_max_turns: int = 40
    fix_max_turns: int = 60
    verify_max_turns: int = 80
    call_timeout: int = 60 * 30  # 单个 claude 进程 30 分钟硬超时
    # 循环节奏
    max_rounds: int = 0          # 0 = 无限
    idle_seconds: int = 300      # 一轮无发现后的休眠
    round_pause: int = 10        # 两轮之间的喘息
    dry_rounds_to_idle: int = 1  # 连续多少轮无发现才进入长休眠
    # 权限模式："skip" = 跳过全部权限确认(full-auto)，否则传给 --permission-mode
    permission_mode: str = "skip"
    # 行为开关
    land: bool = True            # 开 PR 并 merge
    push: bool = True            # 是否推送远端（--no-land 时仍可能想本地集成）
    dry_run: bool = False        # 只审查，不改代码
    # 构建/测试命令（校验阶段会告知 agent）
    build_cmd: str = "go build ./..."
    test_cmd: str = "go test ./..."
    # 工作目录命名
    project_name: str = "project"
    state_dir: Path = field(default=None)  # type: ignore

    def __post_init__(self):
        if self.state_dir is None:
            self.state_dir = self.repo_root / ".perpetual-repair"


# --------------------------------------------------------------------------- #
# 工具：命令执行
# --------------------------------------------------------------------------- #

def run(cmd: list[str], cwd: Optional[Path] = None, timeout: Optional[int] = None,
        check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """跑一个外部命令；返回 CompletedProcess。"""
    LOG.debug("exec: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        check=check,
        text=True,
        capture_output=capture,
    )


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    cp = run(["git", *args], cwd=cwd, check=check)
    if cp.returncode != 0 and check:
        raise RuntimeError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
    return (cp.stdout or "").strip()


# --------------------------------------------------------------------------- #
# 工具：调用 claude headless
# --------------------------------------------------------------------------- #

def call_claude(prompt: str, cwd: Path, cfg: Config, *,
                allowed_tools: Optional[list[str]] = None,
                model: str = "",
                max_turns: int = 40,
                label: str = "claude") -> tuple[bool, str]:
    """
    跑一个 claude headless 进程。返回 (ok, result_text)。
    用 --output-format json 拿到结构化结果里的 .result 文本。
    """
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns)]
    if cfg.permission_mode == "skip":
        cmd += ["--dangerously-skip-permissions"]
    else:
        cmd += ["--permission-mode", cfg.permission_mode]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    # 不加 --add-dir：每个 agent 只在自己的 cwd（worktree）里活动，
    # worktree 已共享 .git，无需额外授权主仓库目录，避免削弱隔离。

    t0 = time.time()
    try:
        cp = run(cmd, cwd=cwd, timeout=cfg.call_timeout, check=False)
    except subprocess.TimeoutExpired:
        LOG.error("[%s] 超时 (%ss)", label, cfg.call_timeout)
        return False, ""
    dt = time.time() - t0

    if cp.returncode != 0:
        LOG.error("[%s] claude 退出码 %s (%.0fs): %s",
                  label, cp.returncode, dt, (cp.stderr or "")[:500])
        return False, cp.stdout or ""

    text = cp.stdout or ""
    # --output-format json：尝试解析出 .result
    try:
        obj = json.loads(text)
        result = obj.get("result", text) if isinstance(obj, dict) else text
        is_error = isinstance(obj, dict) and obj.get("is_error", False)
        LOG.info("[%s] 完成 (%.0fs)%s", label, dt, " [error]" if is_error else "")
        return (not is_error), str(result)
    except json.JSONDecodeError:
        LOG.info("[%s] 完成 (%.0fs, 非 JSON 输出)", label, dt)
        return True, text


# --------------------------------------------------------------------------- #
# 心跳：每 <=30s 打一次进度，证明脚本还活着
# --------------------------------------------------------------------------- #

class Heartbeat:
    def __init__(self, interval: int = 30):
        self.interval = interval
        self._status = "idle"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None
        self._since = time.time()

    def set(self, status: str):
        with self._lock:
            self._status = status
            self._since = time.time()

    def _loop(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                elapsed = int(time.time() - self._since)
                LOG.info("♥ 进行中: %s (已 %ds)", self._status, elapsed)

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1)


HB = Heartbeat(interval=30)


# --------------------------------------------------------------------------- #
# Worktree 生命周期
# --------------------------------------------------------------------------- #

def slug(s: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in s.lower())
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep.strip("-")[:30] or "task"


def add_worktree(cfg: Config, name: str, branch: str) -> Path:
    """在主仓库同级目录建 worktree + 新分支（基于 base_branch）。"""
    path = cfg.repo_root.parent / f"{cfg.project_name}-{name}-workspace"
    # 先清理可能残留的同名 worktree / 分支
    remove_worktree(cfg, path, branch)
    with _GIT_WT_LOCK:
        git(["worktree", "add", str(path), "-b", branch, cfg.base_branch], cwd=cfg.repo_root)
    return path


def remove_worktree(cfg: Config, path: Path, branch: Optional[str] = None):
    with _GIT_WT_LOCK:
        run(["git", "worktree", "remove", "--force", str(path)], cwd=cfg.repo_root, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if branch:
            run(["git", "branch", "-D", branch], cwd=cfg.repo_root, check=False)
        run(["git", "worktree", "prune"], cwd=cfg.repo_root, check=False)


def update_local_main(cfg: Config):
    """把本地 base 分支 ref 快进到远端，不切换工作目录、不碰用户的工作区。"""
    if not cfg.push:
        return
    cp = run(["git", "fetch", cfg.remote,
              f"{cfg.base_branch}:{cfg.base_branch}"], cwd=cfg.repo_root, check=False)
    if cp.returncode == 0:
        LOG.info("本地 %s 已同步到 %s/%s", cfg.base_branch, cfg.remote, cfg.base_branch)
        return
    err = (cp.stderr or "").strip()
    # 若 base 分支正被某个工作区检出，git 会拒绝直接更新其 ref（避免破坏工作树）。
    # 这是预期内的安全拒绝：不强更，提示用户自行 `git pull`，避免污染其工作区。
    if "checked out" in err or "refusing" in err:
        LOG.warning("本地 %s 正被检出，跳过自动快进；需要时请手动 `git pull`",
                    cfg.base_branch)
    else:
        # 非快进（本地领先/分叉）等情况一并忽略，避免破坏用户状态
        LOG.warning("同步本地 %s 跳过: %s", cfg.base_branch, err[:200])


# --------------------------------------------------------------------------- #
# 阶段一：审查
# --------------------------------------------------------------------------- #

AUDIT_SCHEMA_HINT = """{
  "findings": [
    {
      "id": "short-kebab-id",
      "title": "一句话问题标题",
      "severity": "high | medium | low",
      "category": "bug | security | concurrency | resource-leak | correctness | perf",
      "files": ["相关文件路径"],
      "detail": "问题的具体描述与触发条件",
      "fix_hint": "建议的修复方向"
    }
  ]
}"""


def phase_audit(cfg: Config, findings_path: Path) -> list[dict]:
    HB.set("审查仓库")
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    if findings_path.exists():
        findings_path.unlink()

    prompt = f"""你是一名资深代码审查员。请审查这个 Go 仓库，找出**真实存在、可独立修复**的问题。
优先级：正确性 bug > 并发/竞态 > 资源泄漏 > 安全 > 性能。忽略纯风格问题。

要求：
1. 只报你有把握、能定位到具体文件与代码的问题；不要臆测。
2. 每个 finding 必须是一个能由单个修复者在不依赖其他 finding 的前提下独立完成的工作单元。
3. 最多报 {cfg.max_findings_per_round} 个，按严重程度排序，取最该先修的。
4. 把结果**写入文件** `{findings_path}`，JSON 格式，schema 如下：

{AUDIT_SCHEMA_HINT}

如果确实没有发现值得修的问题，就写入 {{"findings": []}}。
只输出审查与写文件，不要改动任何源代码。"""

    ok, _ = call_claude(
        prompt, cwd=cfg.repo_root, cfg=cfg,
        allowed_tools=["Read", "Grep", "Glob", "Bash", "Write"],
        model=cfg.audit_model, max_turns=cfg.audit_max_turns, label="audit",
    )
    if not ok:
        LOG.warning("审查阶段返回异常，本轮按无发现处理")
        return []

    if not findings_path.exists():
        LOG.warning("审查未生成 findings 文件，本轮按无发现处理")
        return []
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        findings = data.get("findings", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError) as e:
        LOG.warning("findings 解析失败: %s", e)
        return []

    # 兜底：补 id、截断数量
    out = []
    for i, f in enumerate(findings[: cfg.max_findings_per_round]):
        if not isinstance(f, dict):
            continue
        f.setdefault("id", f"finding-{i+1}")
        f["id"] = slug(str(f["id"]))
        f.setdefault("title", f["id"])
        out.append(f)
    return out


# --------------------------------------------------------------------------- #
# 阶段二：并行修复
# --------------------------------------------------------------------------- #

@dataclass
class FixResult:
    finding: dict
    branch: str
    worktree: Path
    ok: bool
    committed: bool
    note: str = ""


def phase_fix_one(cfg: Config, finding: dict, round_id: str, idx: int) -> FixResult:
    fid = finding["id"]
    name = f"autofix-{round_id}-{idx}-{fid}"[:40]
    branch = f"auto/fix-{round_id}-{idx}-{fid}"[:60]
    HB.set(f"修复 {fid}")
    wt = add_worktree(cfg, name, branch)

    prompt = f"""你在一个隔离的 git worktree 里，负责修复**下面这一个**问题，不要顺手改别的。

问题：
- 标题: {finding.get('title')}
- 严重度: {finding.get('severity')}
- 类别: {finding.get('category')}
- 相关文件: {', '.join(finding.get('files', []))}
- 描述: {finding.get('detail')}
- 修复方向: {finding.get('fix_hint')}

要求：
1. 用最小、聚焦的改动修复这个问题；遵循仓库既有代码风格，注释按生产风格写、不要出现"教学/示例"等措辞。
2. 改完用 `{cfg.build_cmd}` 确认能编译通过；如相关有测试，尽量跑一下。
3. 编译通过后**提交**：`git add -A && git commit`，提交信息用 Conventional Commits（如 `fix(...): ...`），
   作者用当前仓库默认配置即可。
4. 如果你判断这个"问题"其实不成立或无需修改，**不要提交**，并在最后一行输出 `NO_CHANGE: <原因>`。

只在当前目录工作。"""

    ok, text = call_claude(
        prompt, cwd=wt, cfg=cfg,
        allowed_tools=["Read", "Edit", "Write", "Grep", "Glob", "Bash"],
        model=cfg.fix_model, max_turns=cfg.fix_max_turns, label=f"fix:{fid}",
    )
    # 判断是否真的产生了提交（worktree 上 branch 是否领先 base）
    committed = False
    try:
        ahead = git(["rev-list", "--count", f"{cfg.base_branch}..{branch}"], cwd=cfg.repo_root, check=False)
        committed = ahead.isdigit() and int(ahead) > 0
    except Exception:
        committed = False

    note = ""
    if "NO_CHANGE" in (text or ""):
        note = "agent 判定无需修改"
    return FixResult(finding=finding, branch=branch, worktree=wt,
                     ok=ok, committed=committed, note=note)


def phase_fix_all(cfg: Config, findings: list[dict], round_id: str) -> list[FixResult]:
    HB.set(f"并行修复 {len(findings)} 项 (并发 {cfg.max_workers})")
    results: list[FixResult] = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futs = {
            ex.submit(phase_fix_one, cfg, f, round_id, i): f
            for i, f in enumerate(findings)
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                f = futs[fut]
                LOG.error("修复 %s 抛异常: %s", f.get("id"), e)
                results.append(FixResult(finding=f, branch="", worktree=Path("/nonexistent"),
                                         ok=False, committed=False, note=str(e)))
    return results


# --------------------------------------------------------------------------- #
# 阶段三：集成（把各修复分支合到一个集成分支）
# --------------------------------------------------------------------------- #

def phase_integrate(cfg: Config, fixes: list[FixResult], round_id: str
                    ) -> tuple[Optional[Path], Optional[str], list[FixResult], list[FixResult]]:
    landed = [f for f in fixes if f.committed]
    if not landed:
        return None, None, [], []

    HB.set("集成修复分支")
    int_name = f"repair-{round_id}"
    int_branch = f"auto/repair-{round_id}"
    int_wt = add_worktree(cfg, int_name, int_branch)

    merged: list[FixResult] = []
    skipped: list[FixResult] = []
    for fx in landed:
        cp = run(["git", "merge", "--no-ff", "--no-edit", fx.branch],
                 cwd=int_wt, check=False)
        if cp.returncode == 0:
            merged.append(fx)
        else:
            # 冲突：放弃这条，留到后续轮次（不让一个冲突毁掉整轮）
            run(["git", "merge", "--abort"], cwd=int_wt, check=False)
            skipped.append(fx)
            LOG.warning("集成冲突，跳过 %s（留待下轮）", fx.finding.get("id"))

    if not merged:
        remove_worktree(cfg, int_wt, int_branch)
        return None, None, [], skipped
    return int_wt, int_branch, merged, skipped


# --------------------------------------------------------------------------- #
# 阶段四：校验 + 维修
# --------------------------------------------------------------------------- #

def phase_verify(cfg: Config, int_wt: Path) -> bool:
    HB.set("校验 + 维修")
    prompt = f"""你在一个集成了本轮多个修复的 git worktree 里，负责**校验并修好**它，确保可交付。

步骤：
1. 跑 `{cfg.build_cmd}`，必须通过。
2. 跑 `{cfg.test_cmd}`；如果有失败，定位并修复（可以是这些修复引入的回归，也可能是合并交互导致）。
3. 反复"修复→重跑"，直到 build 通过且测试通过（或确认失败与本轮改动无关、本就存在）。
4. 期间的修复改动要 `git add -A && git commit`，提交信息用 `fix(verify): ...`。
5. 最后输出一行总结：`VERIFY_OK` 或 `VERIFY_FAIL: <无法解决的原因>`。

只在当前目录工作。"""
    ok, text = call_claude(
        prompt, cwd=int_wt, cfg=cfg,
        allowed_tools=["Read", "Edit", "Write", "Grep", "Glob", "Bash"],
        model=cfg.verify_model, max_turns=cfg.verify_max_turns, label="verify",
    )
    if not ok:
        return False
    if "VERIFY_FAIL" in (text or ""):
        LOG.warning("校验未通过: %s", (text or "")[-300:])
        return False
    # 双保险：脚本自己再跑一次 build
    if cfg.build_cmd:
        cp = run(shlex.split(cfg.build_cmd), cwd=int_wt, check=False, timeout=cfg.call_timeout)
        if cp.returncode != 0:
            LOG.warning("脚本侧 build 复核失败: %s", (cp.stderr or "")[-300:])
            return False
    return True


# --------------------------------------------------------------------------- #
# 阶段五：开 PR 并合并
# --------------------------------------------------------------------------- #

def phase_land(cfg: Config, int_wt: Path, int_branch: str,
               merged: list[FixResult], round_id: str) -> bool:
    titles = "\n".join(f"- {f.finding.get('title')}" for f in merged)
    body = f"""自动修复轮次 `{round_id}`，包含 {len(merged)} 项修复：

{titles}

由 perpetual_repair.py 编排：审查 → 并行修复 → 集成 → 校验+维修 自动生成。
"""
    title = f"fix(auto-repair): round {round_id} ({len(merged)} fixes)"

    if not cfg.push:
        LOG.info("--no-land/本地模式：集成分支 %s 已就绪，不推送、不开 PR", int_branch)
        return True

    HB.set("推送 + 开 PR + 合并")
    cp = run(["git", "push", "-u", cfg.remote, int_branch], cwd=int_wt, check=False)
    if cp.returncode != 0:
        LOG.error("push 失败: %s", (cp.stderr or "").strip()[:300])
        return False

    body_file = cfg.state_dir / f"pr-body-{round_id}.md"
    body_file.write_text(body, encoding="utf-8")
    cp = run(["gh", "pr", "create", "--base", cfg.base_branch, "--head", int_branch,
              "--title", title, "--body-file", str(body_file)],
             cwd=int_wt, check=False)
    if cp.returncode != 0:
        LOG.error("gh pr create 失败: %s", (cp.stderr or "").strip()[:300])
        return False
    LOG.info("PR 已创建: %s", (cp.stdout or "").strip())

    cp = run(["gh", "pr", "merge", int_branch, "--squash", "--delete-branch", "--admin"],
             cwd=int_wt, check=False)
    if cp.returncode != 0:
        # 退一步：不加 --admin 再试（仓库可能不需要/不允许 admin override）
        cp = run(["gh", "pr", "merge", int_branch, "--squash", "--delete-branch"],
                 cwd=int_wt, check=False)
    if cp.returncode != 0:
        LOG.error("gh pr merge 失败: %s", (cp.stderr or "").strip()[:300])
        return False
    LOG.info("PR 已合并到 %s", cfg.base_branch)
    return True


# --------------------------------------------------------------------------- #
# 单轮编排
# --------------------------------------------------------------------------- #

def cleanup_round(cfg: Config, fixes: list[FixResult],
                  int_wt: Optional[Path], int_branch: Optional[str]):
    HB.set("清理 worktree")
    for fx in fixes:
        if fx.worktree and str(fx.worktree) != "/nonexistent":
            remove_worktree(cfg, fx.worktree, fx.branch)
    if int_wt:
        remove_worktree(cfg, int_wt, int_branch)
    run(["git", "worktree", "prune"], cwd=cfg.repo_root, check=False)


def run_round(cfg: Config, round_no: int, round_id: str) -> int:
    """跑一整轮。返回本轮真正落地（合并/集成成功）的修复数。"""
    LOG.info("=" * 60)
    LOG.info("第 %s 轮开始 (id=%s)", round_no, round_id)
    findings_path = cfg.state_dir / f"findings-{round_id}.json"

    findings = phase_audit(cfg, findings_path)
    LOG.info("审查发现 %d 个问题", len(findings))
    for f in findings:
        LOG.info("  - [%s] %s (%s)", f.get("severity"), f.get("title"), f.get("id"))
    if not findings:
        return 0
    if cfg.dry_run:
        LOG.info("--dry-run：仅审查，不修复。计划见 %s", findings_path)
        return 0

    fixes = phase_fix_all(cfg, findings, round_id)
    n_committed = sum(1 for f in fixes if f.committed)
    LOG.info("修复阶段：%d/%d 产生了提交", n_committed, len(fixes))
    for f in fixes:
        if not f.committed:
            LOG.info("  · 未提交 %s: %s", f.finding.get("id"), f.note or "无改动/失败")

    int_wt, int_branch, merged, skipped = phase_integrate(cfg, fixes, round_id)
    if not merged:
        LOG.info("没有可集成的修复，清理后进入下一轮")
        cleanup_round(cfg, fixes, int_wt, int_branch)
        return 0
    LOG.info("集成 %d 项（跳过冲突 %d 项）", len(merged), len(skipped))

    verified = phase_verify(cfg, int_wt)
    if not verified:
        LOG.warning("校验未通过，本轮不落地，清理后进入下一轮")
        cleanup_round(cfg, fixes, int_wt, int_branch)
        return 0

    landed = phase_land(cfg, int_wt, int_branch, merged, round_id)
    if landed:
        update_local_main(cfg)

    cleanup_round(cfg, fixes, int_wt, int_branch)
    LOG.info("第 %s 轮完成：落地 %d 项", round_no, len(merged) if landed else 0)
    return len(merged) if landed else 0


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #

def preflight(cfg: Config) -> bool:
    for tool in ("claude", "git"):
        if not shutil.which(tool):
            LOG.error("缺少必需命令: %s", tool)
            return False
    if cfg.land and cfg.push and not shutil.which("gh"):
        LOG.error("启用了开 PR/合并，但找不到 gh CLI；用 --no-land 可关闭")
        return False
    if not (cfg.repo_root / ".git").exists():
        LOG.error("%s 不是 git 仓库根", cfg.repo_root)
        return False
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return True


def make_round_id(round_no: int) -> str:
    # 不依赖 wall-clock 随机性，用单调计数 + 进程 pid 保证唯一可读
    return f"{os.getpid()}-{round_no:04d}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="永续自修复编排器")
    p.add_argument("--repo", default=os.getcwd(), help="仓库根目录（默认当前目录）")
    p.add_argument("--base-branch", default="main")
    p.add_argument("--remote", default="origin")
    p.add_argument("--max-workers", type=int, default=3, help="并行修复 worker 上限（控内存）")
    p.add_argument("--max-findings", type=int, default=5, help="每轮最多处理的问题数")
    p.add_argument("--max-rounds", type=int, default=0, help="0=无限")
    p.add_argument("--idle-seconds", type=int, default=300, help="无发现后的休眠秒数")
    p.add_argument("--round-pause", type=int, default=10)
    p.add_argument("--audit-model", default="")
    p.add_argument("--fix-model", default="")
    p.add_argument("--verify-model", default="")
    p.add_argument("--build-cmd", default="go build ./...")
    p.add_argument("--test-cmd", default="go test ./...")
    p.add_argument("--no-land", action="store_true", help="只修+校验，不开 PR、不合并")
    p.add_argument("--no-push", action="store_true", help="不推送远端（本地集成）")
    p.add_argument("--dry-run", action="store_true", help="只审查并打印计划")
    p.add_argument("--safe-permission", action="store_true",
                   help="用 acceptEdits 而非跳过全部权限（更安全但可能卡在确认）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    repo_root = Path(args.repo).resolve()
    cfg = Config(
        repo_root=repo_root,
        base_branch=args.base_branch,
        remote=args.remote,
        max_workers=max(1, args.max_workers),
        max_findings_per_round=max(1, args.max_findings),
        max_rounds=args.max_rounds,
        idle_seconds=args.idle_seconds,
        round_pause=args.round_pause,
        audit_model=args.audit_model,
        fix_model=args.fix_model,
        verify_model=args.verify_model,
        build_cmd=args.build_cmd,
        test_cmd=args.test_cmd,
        land=not args.no_land,
        push=not args.no_push,
        dry_run=args.dry_run,
        project_name=repo_root.name,
        permission_mode="acceptEdits" if args.safe_permission else "skip",
    )

    if not preflight(cfg):
        return 2

    def _handle_sigint(signum, frame):
        if _STOP.is_set():
            LOG.warning("再次收到中断，强制退出")
            sys.exit(130)
        LOG.warning("收到中断，将在当前轮结束后停止…（再按一次强制退出）")
        _STOP.set()
    signal.signal(signal.SIGINT, _handle_sigint)

    HB.start()
    LOG.info("永续自修复启动 | repo=%s base=%s 并发=%d land=%s",
             cfg.repo_root, cfg.base_branch, cfg.max_workers, cfg.land)

    round_no = 0
    dry_streak = 0
    total_landed = 0
    try:
        while not _STOP.is_set():
            round_no += 1
            rid = make_round_id(round_no)
            try:
                landed = run_round(cfg, round_no, rid)
            except Exception as e:
                LOG.exception("第 %s 轮异常，跳过: %s", round_no, e)
                landed = 0

            total_landed += landed
            if landed == 0:
                dry_streak += 1
            else:
                dry_streak = 0

            if cfg.max_rounds and round_no >= cfg.max_rounds:
                LOG.info("达到 max-rounds=%d，停止", cfg.max_rounds)
                break
            if _STOP.is_set():
                break

            if dry_streak >= cfg.dry_rounds_to_idle:
                HB.set(f"空闲休眠 {cfg.idle_seconds}s")
                LOG.info("连续 %d 轮无落地，休眠 %ds…", dry_streak, cfg.idle_seconds)
                _interruptible_sleep(cfg.idle_seconds)
            else:
                _interruptible_sleep(cfg.round_pause)
    finally:
        HB.stop()

    LOG.info("已停止。累计落地修复 %d 项，共 %d 轮。", total_landed, round_no)
    return 0


def _interruptible_sleep(seconds: int):
    end = seconds
    step = 1
    while end > 0 and not _STOP.is_set():
        time.sleep(min(step, end))
        end -= step


if __name__ == "__main__":
    raise SystemExit(main())
