#!/usr/bin/env python3
"""ai-light-work / lw.py: 按分支的轻量变更台账 + 确定性复杂度打分（插件核心脚本）。

只依赖 Python 3.8+ 标准库和 git。能用规则算出来的（分级、约束匹配、影响面、
增量范围、条目间冲突）都在这里算；模型只做蒸馏意图、判定约束、写给人看的讲解。

自动（插件钩子调用）:
  hook session-start|prompt|pre-edit|post-edit|stop
技能调用:
  context [--files a b]        改代码前要知道的约束
  next-id [--baseline]         下一个条目编号
  review [C-xxx | all | branch] 改动审阅（默认：最近一条）
  check [C-xxx | all | branch]  回归核对（默认：最近一条）
  score [范围] [--hunks N]     复杂度 L0-L3 + 依据 + 影响面 + 真实 diff 片段（默认：上次收尾点..工作区）
  checkpoint --entry C-xxx     收尾：记录本轮改动归属哪个条目
  ask                          冲突闸门：向用户确认前调用，本轮跳过收尾提醒
  conflicts                    条目之间的重叠、回退、反复修改
  why 文件:行 [...]             这一行是哪条台账改动引入的：意图、约束、根因、后来有没有被推翻
  lint / stats / backfill
  view [--title T] [--open] < report.md   把报告（stdin）渲染成可看图的网页 .light-work/<分支>.html
范围: --since-checkpoint | --ledger | --branch | --entry C-xxx | --base REF

存储（全自动，用户不需要关心）:
  <仓库>/.light-work/<分支>.md     每个分支一份台账（已写入 .git/info/exclude）
  <仓库>/.light-work/<分支>.html   /lw-review 生成的网页（最近一次）
  refs/light-work/start|cp/<分支>          台账起点和收尾点快照（不在任何分支上）
"""
import argparse
import codecs
import datetime as _dt
import fnmatch
from html import escape
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

LEDGER_DIR = ".light-work"           # 每个分支一个文件：.light-work/<分支>.md
CP_PREFIX = "refs/light-work/cp/"        # 收尾点链：每次 closeout 一个提交对象，记录 entry 和增量起点
START_PREFIX = "refs/light-work/start/"  # 台账起点：init 时的工作区快照

DEFAULTS = {
    "module_depth": 2,
    "exclude": [
        "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum", "Cargo.lock",
        "Podfile.lock", "*.min.js", "*.min.css", "*.map", "*.snap",
        "dist/*", "*/dist/*", "build/*", "*/build/*", "*/generated/*", "*.pb.go", "*.g.dart",
        "*.md", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.ico", "*.ttf", "*.woff*",
    ],
    "test_regex": r"(^|/)(__tests__|tests?|spec|e2e)(/|$)|[._-](test|spec)\.[A-Za-z]+$|_test\.go$|Test\.(java|kt|swift)$",
    "schema": ["*migration*", "*migrate*", "*.sql", "*.proto", "*.graphql", "*.gql", "*schema.*",
               "*openapi*", "*swagger*", "*.prisma"],
    "config": ["*.yml", "*.yaml", "*.toml", "*.ini", ".env*", "*.env", "package.json",
               "*tsconfig*.json", "*.config.js", "*.config.ts", "*.config.mjs", "*.gradle",
               "*.gradle.kts", "Dockerfile", "*.properties"],
    # 以 * 结尾 = 词前缀匹配；否则 = 整词（含复数 s）；含中文 = 子串匹配
    "sensitive_high": ["auth*", "login", "logout", "token", "password", "passwd", "secret", "credential*",
                       "permission*", "payment*", "pay", "refund*", "balance", "wallet", "migrat*",
                       "transaction*", "webhook*", "concurren*", "mutex", "encrypt*", "decrypt*",
                       "signature", "支付", "退款", "鉴权", "权限", "迁移", "并发", "加密"],
    "sensitive_medium": ["cache*", "lock", "delete", "drop", "retry", "timeout", "cron", "schedul*",
                         "price", "amount", "order", "quota", "fallback", "debounce", "throttle",
                         "订单", "金额", "价格", "缓存", "重试", "降级"],
    "levels": {"l0_lines": 10, "l1_lines": 50, "l3_lines": 200,
               "l1_files": 3, "l3_files": 8, "l3_modules": 3, "notest_lines": 30},
}

IFACE_RES = [re.compile(p) for p in [
    r"^\s*export\s", r"^\s*(public|protected|open)\s", r"^\s*(interface|type|enum)\s+\w",
    r"^\s*func\s+(\([^)]*\)\s*)?[A-Z]", r"module\.exports", r"^\s*@(Get|Post|Put|Delete|Patch|Request)Mapping",
    r"^\s*(message|service|rpc)\s+\w", r"^\s*(class|def)\s+[A-Za-z]",
]]

DEF_RE = re.compile(
    r"\b(?:function|def|func|class|fun|interface|type|enum)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w{2,})"
    r"|\b(?:const|let|var)\s+([A-Za-z_]\w{2,})\s*=\s*(?:async\s*)?(?:\(|function|[A-Za-z_]\w*\s*=>)")

HEADER_RE = re.compile(
    r"^###\s+([BC]-\d{3,})\s*·\s*(\d{4}-\d{2}-\d{2})\s*·\s*([a-z]+)\s*·\s*(open|settled|dropped)\s*(?:·\s*(.*))?$")
FIELD_RE = re.compile(r"^-\s*([^:：]+)[:：]\s*(.*)$")
CONS_RE = re.compile(r"^\s{2,}-\s*([BC]-\d{3,}\.\d+)\s*(\[全局\])?\s*(.*)$")
ID_RE = re.compile(r"[BC]-\d{3,}(?:\.\d+)?")
TYPES = {"baseline", "bug", "change", "refactor", "chore"}


# ───────────────────────── git helpers ─────────────────────────

def git(*args, env=None, check=True, cwd=None):
    p = subprocess.run(["git"] + list(args), capture_output=True, text=True, env=env, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def git_ok(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True).returncode == 0


def repo_root():
    try:
        return git("rev-parse", "--show-toplevel").strip()
    except RuntimeError:
        die("不在 git 仓库里。")


def current_branch():
    b = git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    if b in ("", "HEAD"):
        sha = git("rev-parse", "--short", "HEAD", check=False).strip() or "nohead"
        return "detached-" + sha
    return b


def slug(branch):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


def has_head():
    return git_ok("rev-parse", "--verify", "-q", "HEAD")


def empty_tree():
    return subprocess.run(["git", "hash-object", "-t", "tree", "--stdin"], input="",
                          capture_output=True, text=True).stdout.strip()


def worktree_tree(root):
    """把当前工作区（含未跟踪、尊重 .gitignore）写成一个 tree 对象，不碰真实 index。"""
    idx = git("rev-parse", "--git-path", "index").strip()
    if not os.path.isabs(idx):
        idx = os.path.join(root, idx)
    fd, tmp = tempfile.mkstemp(prefix="lw-index-")
    os.close(fd)
    try:
        if os.path.exists(idx):
            shutil.copyfile(idx, tmp)
        else:
            os.remove(tmp)
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        git("add", "-A", "--", ".", env=env, cwd=root)  # .light-work 在 diff 阶段统一排除
        return git("write-tree", env=env, cwd=root).strip()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def detect_base(cfg=None):
    sym = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    if sym:
        return sym.rsplit("/", 1)[-1]
    for b in ("main", "master", "develop"):
        if git_ok("rev-parse", "--verify", "-q", b) or git_ok("rev-parse", "--verify", "-q", "origin/" + b):
            return b
    return "main"


def merge_base(base):
    for ref in (base, "origin/" + base):
        if git_ok("rev-parse", "--verify", "-q", ref):
            mb = git("merge-base", "HEAD", ref, check=False).strip()
            if mb:
                return mb
    return None


# ───────────────────────── config / paths ─────────────────────────

def die(msg, code=2):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(code)


def load_cfg(root):
    """不读任何用户配置：所有规则都是内置默认值，用户永远不需要改文件。"""
    return json.loads(json.dumps(DEFAULTS))


def ledger_path(root, branch=None):
    return os.path.join(root, LEDGER_DIR, slug(branch or current_branch()) + ".md")


def page_path(root, branch=None):
    return os.path.join(root, LEDGER_DIR, slug(branch or current_branch()) + ".html")


def today():
    return _dt.date.today().isoformat()


# ───────────────────────── ledger parsing ─────────────────────────

def split_files(val):
    out = []
    for part in re.split(r"[,，;；]\s*", val or ""):
        part = part.strip().strip("`")
        if not part or part.startswith("(") or part.startswith("（"):
            continue
        part = part.split("|")[0].strip()
        part = re.split(r"[#:]", part)[0].strip()
        if part and ("/" in part or "." in part):
            out.append(part)
    return out


def parse_ledger(path):
    entries, errors = [], []
    if not os.path.exists(path):
        return entries, errors
    lines = open(path, encoding="utf-8").read().splitlines()
    cur, cur_field = None, None
    for i, line in enumerate(lines, 1):
        if line.startswith("### "):
            m = HEADER_RE.match(line.strip())
            if not m:
                errors.append("第 %d 行标题格式不对: %s" % (i, line.strip()))
                cur = None
                continue
            cur = {"id": m.group(1), "date": m.group(2), "type": m.group(3), "status": m.group(4),
                   "title": (m.group(5) or "").strip(), "fields": {}, "constraints": [], "line": i}
            if cur["type"] not in TYPES:
                errors.append("%s 类型 %s 不在 %s" % (cur["id"], cur["type"], "/".join(sorted(TYPES))))
            entries.append(cur)
            cur_field = None
            continue
        if cur is None:
            continue
        cm = CONS_RE.match(line)
        if cm and cur_field == "约束":
            cur["constraints"].append({"id": cm.group(1), "global": bool(cm.group(2)),
                                       "text": cm.group(3).strip(), "entry": cur["id"]})
            continue
        fm = FIELD_RE.match(line)
        if fm:
            cur_field = fm.group(1).strip()
            cur["fields"][cur_field] = fm.group(2).strip()
    for e in entries:
        f = e["fields"]
        e["files"] = split_files(f.get("文件", f.get("定稿", "")))  # 「定稿」是旧字段名，兼容
        e["shas"] = [s for s in re.findall(r"\b[0-9a-f]{7,40}\b", f.get("提交", ""))]
        lv = re.search(r"L([0-3])", f.get("级别", ""))
        e["level"] = int(lv.group(1)) if lv else None
        rel = f.get("关联", "")
        sup = []
        for part in re.split(r"[;；]", rel):
            if "supersede" in part or "取代" in part:
                sup += ID_RE.findall(part)
        e["supersedes"] = sup
        e["related"] = [x for x in ID_RE.findall(rel) if x not in sup]
        ver = f.get("验证", "")
        e["unverified"] = [s.strip(" ✗") for s in re.split(r"[｜|]", ver) if "✗" in s]
        e["assumptions"] = f.get("假设", "")
        e["rounds"] = len(set(re.findall(r"\bR(\d+)", f.get("过程", "")))) or (1 if f.get("过程") else 0)
        e["verified"] = [s.strip(" ✓") for s in re.split(r"[｜|]", ver) if "✓" in s]
        for c in e["constraints"]:
            c["files"] = e["files"]
            c["status"] = e["status"]
    return entries, errors


def active_constraints(entries):
    dead = set()
    by_id = {e["id"]: e for e in entries}
    for e in entries:
        for s in e["supersedes"]:
            if "." in s:
                dead.add(s)
            elif s in by_id:
                dead.update(c["id"] for c in by_id[s]["constraints"])
    out = []
    for e in entries:
        if e["status"] == "dropped":
            continue
        for c in e["constraints"]:
            if c["id"] not in dead:
                out.append(c)
    return out, dead


def path_match(path, files):
    """path 是否落在条目登记的文件/目录里。"""
    for f in files:
        f = f.rstrip("/")
        if path == f or path.startswith(f + "/") or fnmatch.fnmatch(path, f):
            return True
        if "/" not in f and os.path.basename(path) == f:
            return True
    return False


def index_line(e):
    sha = e["shas"][-1][:7] if e["shas"] else "未提交"
    if e.get("rounds", 0) > 1:
        sha += " · %d 轮" % e["rounds"]
    lvl = "L%d" % e["level"] if e["level"] is not None else "--"
    return "%s | %s | %s | %s | %s | %s" % (e["id"], e["type"], lvl, e["status"], e["title"] or e["fields"].get("意图", "")[:30], sha)


# ───────────────────────── diff analysis ─────────────────────────

def glob_hit(path, pats):
    base = os.path.basename(path)
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p) for p in pats)


def tokens(s):
    return [t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", s)]


def kw_hits(text, kws):
    toks = set(tokens(text))
    hits = set()
    for k in kws:
        if re.search(r"[^\x00-\x7f]", k):
            if k in text:
                hits.add(k)
        elif k.endswith("*"):
            pre = k[:-1].lower()
            if any(t.startswith(pre) for t in toks):
                hits.add(k[:-1])
        else:
            kl = k.lower()
            if kl in toks or (kl + "s") in toks:
                hits.add(k)
    return hits


def cp_ref(br=None):
    return CP_PREFIX + slug(br or current_branch())


def start_ref(br=None):
    return START_PREFIX + slug(br or current_branch())


def checkpoint_chain(br=None):
    """收尾点链（旧→新）：[{tree, entry, from}]"""
    ref = cp_ref(br)
    if not git_ok("rev-parse", "--verify", "-q", ref):
        return []
    out = git("log", "--first-parent", "--format=%T%x1f%s%x1e", ref)
    chain = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        tree, subj = rec.split("\x1f", 1)
        m_e = re.search(r"entry=(\S+)", subj)
        m_f = re.search(r"from=([0-9a-f]+)", subj)
        if m_f:
            chain.append({"tree": tree, "entry": m_e.group(1) if m_e else None, "from": m_f.group(1)})
    chain.reverse()
    return chain


def head_or_empty():
    return git("rev-parse", "HEAD^{tree}").strip() if has_head() else empty_tree()


def change_entries(entries):
    return [e for e in entries if e["type"] != "baseline" and e["status"] != "dropped"]


def entry_ranges(entries, mode, ref):
    selected = change_entries(entries)
    if mode == "entry":
        if ref is None and selected:
            ref = selected[-1]["id"]
        if ref is None:
            die("当前分支没有可检查的台账改动。")
        selected = [e for e in entries if e["id"] == ref and e["type"] != "baseline"]
        if not selected:
            die("台账没有条目 %s。" % ref)
    ids = {e["id"] for e in selected}
    rounds = [c for c in checkpoint_chain() if c["entry"] in ids and c["from"]]
    recorded = {c["entry"] for c in rounds}
    missing = [e["id"] for e in selected if e["id"] not in recorded]
    if mode == "entry":
        if missing:
            die("%s 还没有收尾点，暂无可归属的 diff。" % ref)
        label = "%s 的 %d 轮修改" % (ref, len(rounds))
    else:
        label = "全部台账改动（%d 条，%d 轮）" % (len(recorded), len(rounds))
        if missing:
            label += " · 暂无收尾点: " + ", ".join(missing)
    return [(c["from"], c["tree"]) for c in rounds], label, selected


def resolve_range(root, cfg, mode, ref=None):
    """返回工作区 diff 的起止点和说明。"""
    br = current_branch()
    cur = worktree_tree(root)
    if mode == "base":
        if not git_ok("rev-parse", "--verify", "-q", ref):
            die("找不到 ref: %s" % ref)
        return [(git("rev-parse", ref).strip(), cur)], "%s..工作区" % ref
    if mode == "branch":
        base = detect_base(cfg)
        mb = merge_base(base)
        if mb:
            return [(mb, cur)], "分支全部（%s 分叉点 %s..工作区）" % (base, mb[:7])
        return [(head_or_empty(), cur)], "找不到 %s，退化为 HEAD..工作区" % base
    if mode == "ledger":
        sr = start_ref(br)
        if git_ok("rev-parse", "--verify", "-q", sr):
            return [(git("rev-parse", sr + "^{tree}").strip(), cur)], "台账范围（台账起点..工作区）"
        return resolve_range(root, cfg, "branch")[0], "台账没有起点记录，退化为整个分支"
    # checkpoint（默认）
    if git_ok("rev-parse", "--verify", "-q", cp_ref(br)):
        return [(git("rev-parse", cp_ref(br) + "^{tree}").strip(), cur)], "上次收尾点..工作区"
    if git_ok("rev-parse", "--verify", "-q", start_ref(br)):
        return [(git("rev-parse", start_ref(br) + "^{tree}").strip(), cur)], "台账起点..工作区"
    return [(head_or_empty(), cur)], "HEAD..工作区（还没有收尾点）"


def merge_diffs(parts):
    out = {}
    for d in parts:
        for p, v in d.items():
            if p not in out:
                out[p] = v
            else:
                o = out[p]
                o["add"] += v["add"]; o["del"] += v["del"]
                o["lines"] += v["lines"]; o["hunks"] += v["hunks"]
    return out


def diff_header_path(value):
    value = value.rstrip("\t")
    if value.startswith('"') and value.endswith('"'):
        # Git 对引号、控制字符等路径使用 C 字符串转义。
        value = codecs.escape_decode(value[1:-1].encode("utf-8"))[0].decode("utf-8", errors="replace")
    return value


def parse_diff(root, base, cur):
    """返回 {path: {"add","del","lines":[(+/-, text)], "hunks":[...]}}"""
    files = {}
    num = git("diff", "--numstat", "-z", "--no-renames", base, cur, "--", ".", ":(exclude)%s" % LEDGER_DIR, cwd=root)
    for ln in num.split("\0"):
        parts = ln.split("\t", 2)
        if len(parts) < 3:
            continue
        a, d, p = parts[0], parts[1], parts[2]
        files[p] = {"add": int(a) if a.isdigit() else 0, "del": int(d) if d.isdigit() else 0,
                    "binary": a == "-", "lines": [], "hunks": []}
    if not files:
        return files
    raw = git("-c", "core.quotePath=false", "diff", "-U2", "--no-renames", "--no-color",
              base, cur, "--", ".", ":(exclude)%s" % LEDGER_DIR, cwd=root)
    curf, hunk = None, None
    for ln in raw.splitlines():
        if ln.startswith("diff --git"):
            curf, hunk = None, None
            continue
        if hunk is None and ln.startswith("+++ "):
            p = diff_header_path(ln[4:])
            if p.startswith("b/"):  # 删除文件时这里是 /dev/null，沿用 --- a/x 的文件名
                curf = p[2:]
            continue
        if hunk is None and ln.startswith("--- "):
            p = diff_header_path(ln[4:])
            if p.startswith("a/"):
                curf = p[2:]
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", ln)
        if m and curf in files:
            new_len = int(m.group(4) or 1)
            start, ln_ = (int(m.group(3)), new_len) if new_len else (int(m.group(1)), int(m.group(2) or 1))  # 纯删除用旧行号
            hunk = {"start": start, "len": ln_, "ctx": m.group(5).strip(), "header": ln,
                    "old_start": int(m.group(1)), "new_start": int(m.group(3)),
                    "old_len": int(m.group(2) or 1), "new_len": new_len,
                    "body": [], "changed": 0}
            files[curf]["hunks"].append(hunk)
            continue
        if hunk is None or curf not in files:
            continue
        hunk["body"].append(ln)
        if ln.startswith(("+", "-")):
            hunk["changed"] += 1
            files[curf]["lines"].append((ln[0], ln[1:]))
    return files


def module_key(path, depth):
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else "."


def analyze(root, cfg, mode="checkpoint", ref=None, nhunks=4, current_entry=None, evidence=False):
    lp = ledger_path(root)
    entries, _ = parse_ledger(lp)
    scoped_entries = entries
    if mode in ("entry", "all_entries"):
        segs, label, scoped_entries = entry_ranges(entries, mode, ref)
    else:
        segs, label = resolve_range(root, cfg, mode, ref)
        if current_entry:
            scoped_entries = [e for e in change_entries(entries) if e["id"] == current_entry]
            if not scoped_entries:
                die("台账没有可用条目 %s。" % current_entry)
            label = "%s 的本轮改动" % current_entry
    parts = []
    for base, end in segs:
        part = parse_diff(root, base, end)
        if evidence:
            for file_diff in part.values():
                for hunk in file_diff["hunks"]:
                    hunk["revision"] = "%s → %s" % (base[:8], end[:8])
        parts.append(part)
    diff = merge_diffs(parts)
    L = cfg["levels"]
    test_re = re.compile(cfg["test_regex"])
    eff_files, test_files, skipped = [], [], []
    for p, d in diff.items():
        if glob_hit(p, cfg["exclude"]) or d["binary"]:
            skipped.append(p)
        elif test_re.search(p):
            test_files.append(p)
        else:
            eff_files.append(p)
    eff_lines = sum(diff[p]["add"] + diff[p]["del"] for p in eff_files)
    eff_del = sum(diff[p]["del"] for p in eff_files)
    modules = sorted(set(module_key(p, cfg["module_depth"]) for p in eff_files))
    schema = [p for p in eff_files if glob_hit(p, cfg["schema"])]
    config = [p for p in eff_files if glob_hit(p, cfg["config"])]

    # 导出/签名：只有"删掉导出"或"签名变了"才算接口变化；常量改值、新增导出不算
    NAME_RE = re.compile(r"(?:function|class|interface|type|enum|const|let|var|def|func|fun|message|service|rpc)"
                         r"\s+(?:\([^)]*\)\s*)?(\w+)")

    def sig(text):
        t = text.strip()
        if "=>" in t:
            return re.sub(r"\s+", "", t.split("=>")[0])
        if "(" in t:
            return re.sub(r"\s+", "", t.split("{")[0].rstrip(":"))
        return re.sub(r"\s+", "", t.split("=")[0])

    groups = {}
    for p in eff_files:
        for sign, text in diff[p]["lines"]:
            if any(r.search(text) for r in IFACE_RES):
                m = NAME_RE.search(text)
                key = (p, m.group(1) if m else text.strip()[:40])
                groups.setdefault(key, {"-": [], "+": []})[sign].append(text)
    iface, new_exports = [], []
    for (p, name), g in groups.items():
        if g["-"] and not g["+"]:
            iface.append("删除 %s: %s" % (p, g["-"][0].strip()[:80]))
        elif g["+"] and not g["-"]:
            new_exports.append("%s: %s" % (p, name))
        elif set(map(sig, g["-"])) != set(map(sig, g["+"])):
            iface.append("签名 %s: %s → %s" % (p, g["-"][0].strip()[:60], g["+"][0].strip()[:60]))

    hi_path, hi_line, md_hits = set(), {}, set()
    for p in eff_files + schema:
        hi_path |= kw_hits(p, cfg["sensitive_high"])
        for _, text in diff.get(p, {}).get("lines", []):
            for k in kw_hits(text, cfg["sensitive_high"]):
                hi_line[k] = hi_line.get(k, 0) + 1
            md_hits |= kw_hits(text, cfg["sensitive_medium"])
        md_hits |= kw_hits(p, cfg["sensitive_medium"])
    hi_strong = sorted(hi_path | {k for k, n in hi_line.items() if n >= 2})
    hi_weak = sorted(set(hi_line) - set(hi_strong))

    # 台账交叉
    act, _ = active_constraints(entries)
    changed = list(diff.keys())
    touched = [c for c in act if not c["global"] and any(path_match(p, c["files"]) for p in changed)]
    globals_ = [c for c in act if c["global"]]
    registered = set()
    for e in entries:
        if e["status"] != "dropped":
            registered.update(e["files"])
    unregistered = [p for p in eff_files if not path_match(p, list(registered))]

    level, reasons = 0, []

    def bump(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append("L%d ← %s" % (lv, why))

    if not diff:
        reasons.append("没有改动")
    elif not eff_files:
        reasons.append("L0 ← 只动了测试/文档/锁文件/资源")
    # 每条信号只按它能达到的最高级别记一次
    if eff_lines >= L["l3_lines"]:
        bump(3, "有效改动 %d 行 ≥ %d" % (eff_lines, L["l3_lines"]))
    elif eff_lines >= L["l1_lines"]:
        bump(2, "有效改动 %d 行 ≥ %d" % (eff_lines, L["l1_lines"]))
    elif eff_lines > L["l0_lines"]:
        bump(1, "有效改动 %d 行 > %d" % (eff_lines, L["l0_lines"]))
    nf = len(eff_files)
    if nf > L["l3_files"]:
        bump(3, "改了 %d 个文件 > %d" % (nf, L["l3_files"]))
    elif nf > L["l1_files"]:
        bump(2, "改了 %d 个文件 > %d" % (nf, L["l1_files"]))
    elif nf > 1:
        bump(1, "改了 %d 个文件" % nf)
    if len(modules) >= L["l3_modules"]:
        bump(3, "跨 %d 个模块: %s" % (len(modules), ", ".join(modules)))
    elif len(modules) >= 2:
        bump(2, "跨 2 个模块: %s" % ", ".join(modules))
    if iface:
        bump(2, "接口变化 %d 处（删除导出/签名改变）" % len(iface))
    if config:
        bump(2, "改了配置: %s" % ", ".join(config[:4]))
    if hi_strong:
        bump(3, "敏感区域: %s" % ", ".join(hi_strong[:6]))
    elif hi_weak:
        bump(2, "零星命中敏感词: %s" % ", ".join(hi_weak))
    if md_hits:
        bump(2, "命中关注词: %s" % ", ".join(sorted(md_hits)[:6]))
    if schema:
        bump(3, "schema/迁移/接口定义: %s" % ", ".join(schema[:4]))
    # 触及约束不抬级别（级别 = 人理解它要花多少力气），但无论哪一级卡片都要带约束核对
    if touched:
        reasons.append("·· 触及台账约束 %s（不抬级别，卡片里必须逐条核对）" % ", ".join(c["id"] for c in touched[:8]))

    # 影响面：删掉/改了签名的定义，用 git grep 实测还有谁在引用
    minus, plus = {}, {}
    for p in eff_files:
        for sign, text in diff[p]["lines"]:
            for m in DEF_RE.finditer(text):
                name = m.group(1) or m.group(2)
                (minus if sign == "-" else plus).setdefault(name, set()).add(p)
    removed = sorted(n for n in minus if n not in plus)
    resig = sorted(n for n in minus if n in plus)
    impact = []
    for n in (removed + resig)[:8]:
        out = git("grep", "-n", "-w", "-I", "--untracked", n, "--", ".", ":(exclude)%s" % LEDGER_DIR, check=False, cwd=root)
        refs = [ln for ln in out.splitlines() if not DEF_RE.search(ln.split(":", 2)[-1])]
        fset = sorted(set(ln.split(":", 1)[0] for ln in refs))
        impact.append({"name": n, "kind": "删除" if n in removed else "签名变化", "refs": len(refs),
                       "files": fset[:8], "sample": refs[:3]})

    # 一层调用方：签名没变、只改了内部逻辑的函数，看本轮没改的文件里谁按名字引用了它。
    # 只做一层、只展示不抬级别：同名误报难免，交给模型在卡片里判断要不要提。
    callers = []
    skip = set(removed + resig) | {"for", "while", "switch", "catch", "return", "function", "constructor", "(文件级)"}
    syms = []
    for p in eff_files[:10]:
        for b, c in segs:
            for n in changed_symbols(root, b, c, p):
                if n and n not in skip and len(n) >= 4 and n not in syms:
                    syms.append(n)
    for n in syms[:8]:
        out = git("grep", "-l", "-w", "-I", "--untracked", n, "--", ".", ":(exclude)%s" % LEDGER_DIR, check=False, cwd=root)
        fs = [f for f in out.splitlines() if f and f not in diff and not test_re.search(f)]
        if fs:
            callers.append({"name": n, "files": fs[:5], "n": len(fs)})

    flags = []
    for it in impact:
        if it["kind"] == "删除" and it["refs"]:
            flags.append("删除了 %s，但仍有 %d 处引用: %s" % (it["name"], it["refs"], ", ".join(it["files"][:4])))
    if eff_files and not test_files and eff_lines >= L["notest_lines"]:
        flags.append("改了 %d 行逻辑但没有任何测试改动" % eff_lines)
    if eff_del >= 20 and eff_del > 2 * (eff_lines - eff_del):
        flags.append("以删除为主（删 %d 行）——确认没有删掉别人依赖的分支" % eff_del)
    if unregistered and entries:
        flags.append("有 %d 个文件不属于任何台账条目" % len(unregistered))

    hunks = []
    for p in eff_files:
        for h in diff[p]["hunks"]:
            hunks.append((h["changed"], p, h))
    hunks.sort(key=lambda x: -x[0])
    top = []
    for _, p, h in hunks[:nhunks]:
        body = h["body"][:24]
        top.append({"loc": "%s:%d-%d" % (p, h["start"], h["start"] + max(h["len"], 1) - 1),
                    "ctx": h["ctx"], "changed": h["changed"], "body": body,
                    "truncated": len(h["body"]) > 24})

    return {
        "branch": current_branch(), "range": label, "level": level, "reasons": reasons, "flags": flags,
        "eff_lines": eff_lines, "eff_files": eff_files, "test_files": test_files, "skipped": skipped,
        "modules": modules, "iface": iface[:12], "new_exports": new_exports[:12], "schema": schema, "config": config,
        "sensitive": {"strong": hi_strong, "weak": hi_weak, "medium": sorted(md_hits)},
        "impact": impact, "callers": callers, "touched_constraints": touched, "global_constraints": globals_, "unregistered": unregistered,
        "entries": scoped_entries, "hunks": top, "ledger": os.path.relpath(lp, root),
        "evidence": diff if evidence else None,
    }


# ───────────────────────── commands ─────────────────────────

LEDGER_HEAD = """# 变更台账 · {branch}

> 基准分支: {base} · 创建: {date} · 由 ai-light-work 自动维护，切换分支会自动换成那个分支自己的台账。
> 规矩: open 条目可以续改；提交后自动转 settled，之后只追加不改写；需求变了开新条目并写 `关联: supersedes C-xxx.n`。

"""


def ensure_exclude(root):
    ex = git("rev-parse", "--git-path", "info/exclude").strip()
    if not os.path.isabs(ex):
        ex = os.path.join(root, ex)
    os.makedirs(os.path.dirname(ex), exist_ok=True)
    txt = open(ex, encoding="utf-8").read() if os.path.exists(ex) else ""
    if "/%s/" % LEDGER_DIR not in txt.split():
        with open(ex, "a", encoding="utf-8") as f:
            f.write("\n# ai-light-work（个人台账，不进团队仓库）\n/%s/\n" % LEDGER_DIR)


HOOK_LINE = 'python3 "$HOME/.light-work/lw.py" backfill 2>/dev/null || true  # ai-light-work'


def install_hook(root):
    if git("config", "--get", "core.hooksPath", check=False).strip():
        print("  · 检测到 core.hooksPath（husky 等），跳过 post-commit 钩子（提交号改由下次收尾时补）")
        return
    hp = git("rev-parse", "--git-path", "hooks/post-commit").strip()
    if not os.path.isabs(hp):
        hp = os.path.join(root, hp)
    txt = open(hp, encoding="utf-8").read() if os.path.exists(hp) else "#!/bin/sh\n"
    if "ai-light-work" in txt:
        return
    os.makedirs(os.path.dirname(hp), exist_ok=True)
    with open(hp, "w", encoding="utf-8") as f:
        f.write(txt.rstrip("\n") + "\n" + HOOK_LINE + "\n")
    os.chmod(hp, 0o755)
    print("  · 已安装 post-commit 钩子（提交后自动把 SHA 回填到台账）")


def make_commit(tree, msg, *parents):
    args = ["commit-tree", tree, "-m", msg]
    for parent in parents:
        if parent:
            args += ["-p", parent]
    env = dict(os.environ)
    for k, v in (("GIT_AUTHOR_NAME", "ai-light-work"), ("GIT_AUTHOR_EMAIL", "light-work@local"),
                 ("GIT_COMMITTER_NAME", "ai-light-work"), ("GIT_COMMITTER_EMAIL", "light-work@local")):
        env.setdefault(k, v)
    return git(*args, env=env).strip()


def init_ledger(root, cfg, quiet=False, hook=True):
    """幂等：建分支目录、本地 exclude、post-commit 钩子、台账文件、台账起点快照。返回 (台账路径, 是否新建)。"""
    say = (lambda *x: None) if quiet else print
    br = current_branch()
    os.makedirs(os.path.join(root, LEDGER_DIR), exist_ok=True)
    ensure_exclude(root)
    if hook:
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                install_hook(root)
        except Exception as e:
            note_error("安装 post-commit 钩子", e)
    base = detect_base()
    lp = ledger_path(root, br)
    created = False
    if not os.path.exists(lp):
        with open(lp, "w", encoding="utf-8") as f:
            f.write(LEDGER_HEAD.format(branch=br, base=base, date=today()))
        created = True
        say("✓ 已创建 %s（基准 %s）" % (os.path.relpath(lp, root), base))
    else:
        say("已存在: %s" % os.path.relpath(lp, root))
    if not git_ok("rev-parse", "--verify", "-q", start_ref(br)):
        tree = worktree_tree(root)
        git("update-ref", start_ref(br), make_commit(tree, "light-work start %s" % today()))
    return lp, created


def cmd_init(a):
    root = repo_root()
    init_ledger(root, load_cfg(root))


def cmd_context(a):
    root = repo_root()
    cfg = load_cfg(root)
    br = current_branch()
    base = detect_base(cfg)
    lp = ledger_path(root, br)
    print("当前分支状态（每次从台账实时生成，相当于一份不会过期的 handoff）")
    print("分支: %s  基准: %s  台账: %s" % (br, base, os.path.relpath(lp, root)))
    if not os.path.exists(lp):
        print("还没有台账（开新会话时插件会自动创建）。")
        return
    entries, errors = parse_ledger(lp)
    if errors:
        print("⚠ 台账格式问题 %d 处（运行 lint 查看）" % len(errors))
    act, _ = active_constraints(entries)
    files = a.files or []
    if files:
        rel = [c for c in act if c["global"] or any(path_match(f, c["files"]) for f in files)]
        print("\n## 与 %s 相关的有效约束（%d/%d）" % (", ".join(files), len(rel), len(act)))
    else:
        rel = act
        print("\n## 有效约束（%d）" % len(act))
    for c in rel[:a.limit]:
        tag = "[全局]" if c["global"] else ""
        tent = "（open，未定稿）" if c["status"] == "open" else ""
        print("- %s%s %s%s  ⟵ %s" % (c["id"], tag, c["text"], tent, ", ".join(c["files"][:3]) or "-"))
    if len(rel) > a.limit:
        print("… 还有 %d 条，用 --files 缩小范围" % (len(rel) - a.limit))
    opens = [e for e in entries if e["status"] == "open"]
    if opens:
        print("\n## 未定稿（open）——新需求如果是同一意图的续改，就更新它而不是新开")
        for e in opens:
            print("- %s %s · 意图: %s" % (e["id"], e["title"], e["fields"].get("意图", "")))
            if e["unverified"]:
                print("  未验证: %s" % "；".join(e["unverified"]))
            if e["assumptions"]:
                print("  假设: %s" % e["assumptions"])
    print("\n## 最近条目")
    for e in entries[-8:]:
        print("- " + index_line(e))
    unv = [(e["id"], u) for e in entries if e["status"] != "dropped" for u in e["unverified"]]
    if unv:
        print("\n## 累计未验证 %d 项（不要把“没验证”当成“没问题”）" % len(unv))
        for i, u in unv[-6:]:
            print("- %s: %s" % (i, u))


def cmd_next_id(a):
    root = repo_root()
    entries, _ = parse_ledger(ledger_path(root))
    pre = "B" if a.baseline else "C"
    nums = [int(e["id"].split("-")[1]) for e in entries if e["id"].startswith(pre)]
    start = 0 if pre == "B" else 1
    print("%s-%03d" % (pre, (max(nums) + 1) if nums else start))


def print_analysis(r, show_hunks=True, check_mode=False):
    print("范围: %s · 分支: %s" % (r["range"], r["branch"]))
    if not r["eff_files"] and not r["test_files"] and not r["skipped"]:
        print("没有改动。")
        return
    print("级别: L%d   （有效 %d 行 / %d 文件 / 模块 %s）" % (
        r["level"], r["eff_lines"], len(r["eff_files"]), ", ".join(r["modules"]) or "-"))
    for x in r["reasons"]:
        print("  · " + x)
    for x in r["flags"]:
        print("  ⚠ " + x)
    print("\n文件: %s" % (", ".join(r["eff_files"]) or "-"))
    if r["test_files"]:
        print("测试: %s" % ", ".join(r["test_files"]))
    if r["skipped"]:
        print("忽略: %s" % ", ".join(r["skipped"][:8]))
    if r["iface"]:
        print("\n接口变化:")
        for x in r["iface"]:
            print("  " + x)
    if r.get("new_exports"):
        print("新增导出: %s" % ", ".join(r["new_exports"]))
    if r.get("impact"):
        print("\n影响面（git grep 实测，不含定义行）:")
        for it in r["impact"]:
            if it["kind"] != "删除" and not it["refs"]:
                continue
            print("  - %s %s → %d 处引用 %s" % (it["kind"], it["name"], it["refs"], ", ".join(it["files"]) or ""))
    if r.get("callers"):
        print("\n一层调用方（改了这些函数的内部逻辑；下面这些本轮没改的文件按名字引用了它们，可能有同名误报）:")
        for c in r["callers"]:
            more = " 等 %d 个文件" % c["n"] if c["n"] > len(c["files"]) else ""
            print("  - %s → %s%s" % (c["name"], ", ".join(c["files"]), more))
    tc = r["touched_constraints"]
    if tc or r["global_constraints"]:
        print("\n必须核对的约束（%d 触及 + %d 全局）:" % (len(tc), len(r["global_constraints"])))
        for c in tc:
            print("  - %s %s  ⟵ %s%s" % (c["id"], c["text"], ", ".join(c["files"][:3]),
                                        "（open）" if c["status"] == "open" else ""))
        for c in r["global_constraints"]:
            print("  - %s[全局] %s" % (c["id"], c["text"]))
    if check_mode:
        ents = [e for e in r["entries"] if e["status"] != "dropped"]
        opens = [e for e in ents if e["status"] == "open"]
        unv = [(e["id"], u) for e in ents for u in e["unverified"]]
        nosha = [e["id"] for e in ents if e["type"] != "baseline" and not e["shas"]]
        asm = [(e["id"], e["assumptions"]) for e in ents if e["assumptions"] and e["assumptions"] not in ("无", "-")]
        if opens:
            print("\n未定稿 open: %s" % ", ".join("%s %s" % (e["id"], e["title"]) for e in opens))
        if unv:
            print("未验证项:")
            for i, u in unv:
                print("  - %s: %s" % (i, u))
        if asm:
            print("未确认的假设:")
            for i, s in asm:
                print("  - %s: %s" % (i, s))
        if nosha:
            print("还没关联提交: %s" % ", ".join(nosha))
        if r["unregistered"]:
            print("未登记改动（不属于任何条目，可能是顺手改/带出来的）: %s" % ", ".join(r["unregistered"]))
    if show_hunks and r["hunks"]:
        print("\n关键片段候选（真实 diff，按改动量排序；卡片只能从这里引用）:")
        for h in r["hunks"]:
            print("\n▸ %s  %s（%d 行变动）" % (h["loc"], h["ctx"], h["changed"]))
            for b in h["body"]:
                print("    " + b)
            if h["truncated"]:
                print("    …")
    print("\n台账: %s" % r["ledger"])


def strip_for_json(r):
    out = dict(r)
    out.pop("evidence", None)
    out["entries"] = [{k: e[k] for k in ("id", "type", "status", "title", "level", "shas", "unverified", "files")}
                      for e in r["entries"]]
    return out


def parse_scope(value):
    if value in ("all", "a"):
        return "all_entries", None
    if value in ("branch", "b"):
        return "branch", None
    if re.fullmatch(r"C-\d{3,}", value):
        return "entry", value
    raise argparse.ArgumentTypeError("范围须为 C-xxx、all 或 branch")


def parse_view_scope(value):
    return ("entry", None) if value == "latest" else parse_scope(value)


def pick_mode(a, default):
    if getattr(a, "scope", None):
        return a.scope
    if getattr(a, "entry", None):
        return "entry", a.entry
    if getattr(a, "base", None):
        return "base", a.base
    if getattr(a, "latest_entry", False):
        return "entry", None
    for m in ("branch", "ledger", "since_checkpoint", "all_entries"):
        if getattr(a, m, False):
            return ("checkpoint" if m == "since_checkpoint" else m), None
    return default, None


def cmd_score(a):
    root = repo_root()
    cfg = load_cfg(root)
    mode, ref = pick_mode(a, "checkpoint" if a.cmd == "score" else "entry")
    r = analyze(root, cfg, mode, ref, a.hunks, a.current_entry)
    if a.json:
        print(json.dumps(strip_for_json(r), ensure_ascii=False, indent=1))
    else:
        print_analysis(r, show_hunks=a.hunks > 0)


def cmd_check(a):
    root = repo_root()
    cfg = load_cfg(root)
    mode, ref = pick_mode(a, "entry")
    r = analyze(root, cfg, mode, ref, a.hunks, a.current_entry)
    _, errors = parse_ledger(ledger_path(root))
    if a.json:
        print(json.dumps(strip_for_json(r), ensure_ascii=False, indent=1))
    else:
        print_analysis(r, show_hunks=a.hunks > 0, check_mode=True)
        if errors:
            print("\n台账格式问题:")
            for e in errors:
                print("  - " + e)


def state_path(root):
    d = git("rev-parse", "--git-path", "light-work").strip()
    if not os.path.isabs(d):
        d = os.path.join(root, d)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state-%s.json" % slug(current_branch()))


def state_load(root):
    try:
        return json.load(open(state_path(root), encoding="utf-8"))
    except Exception:
        return {"dirty": False, "ctx_shown": {}}


def state_save(root, st):
    json.dump(st, open(state_path(root), "w", encoding="utf-8"), ensure_ascii=False)


def state_clear(root):
    st = state_load(root)
    st.update({"dirty": False, "nag_tree": None, "awaiting": False})
    state_save(root, st)


def cmd_checkpoint(a):
    root = repo_root()
    cfg = load_cfg(root)
    br = current_branch()
    segs, _ = resolve_range(root, cfg, "base" if a.base else "checkpoint", a.base)
    frm, tree = segs[0]
    frm_tree = git("rev-parse", frm + "^{tree}").strip()
    if frm_tree == tree and not a.force:
        print("没有新改动，收尾点不变。")
        state_clear(root)
        return
    ref = cp_ref(br)
    parent = git("rev-parse", ref).strip() if git_ok("rev-parse", "--verify", "-q", ref) else None
    base_parent = None
    if a.base and (not parent or frm_tree != git("rev-parse", parent + "^{tree}").strip()):
        # 起点可能包含本轮前已有的改动，保留该 tree 供后续按条目取 diff。
        base_parent = make_commit(frm_tree, "light-work round start")
    msg = "ledger checkpoint entry=%s from=%s at=%s" % (a.entry or "-", frm_tree,
                                                         _dt.datetime.now().isoformat(timespec="seconds"))
    git("update-ref", ref, make_commit(tree, msg, parent, base_parent))
    state_clear(root)
    print("✓ 收尾点已记录（%s%s）。下次 score 默认只看这之后的增量。" % (
        tree[:7], "，归属 " + a.entry if a.entry else ""))


def cmd_snapshot(a):
    print(worktree_tree(repo_root()))


def cmd_lint(a):
    root = repo_root()
    lp = ledger_path(root)
    if not os.path.exists(lp):
        die("没有台账: %s" % os.path.relpath(lp, root))
    entries, errors = parse_ledger(lp)
    ids = [e["id"] for e in entries]
    all_ids = set(ids) | {c["id"] for e in entries for c in e["constraints"]}
    seen = set()
    for e in entries:
        if e["id"] in seen:
            errors.append("%s 编号重复" % e["id"])
        seen.add(e["id"])
        f = e["fields"]
        for need in ("意图", "验证"):
            if need not in f:
                errors.append("%s 缺少「%s」" % (e["id"], need))
        if e["type"] != "baseline" and "级别" not in f:
            errors.append("%s 缺少「级别」（记录阶段写「待定」）" % e["id"])
        if e["status"] == "settled" and e["type"] != "baseline" and e["level"] is None:
            errors.append("%s 已 settled 但级别还是待定" % e["id"])
        if e["status"] == "settled" and e["type"] != "baseline" and not e["files"]:
            errors.append("%s 已 settled 但「文件」没写" % e["id"])
        if e["level"] is not None and e["level"] >= 1 and not e["constraints"] and e["type"] != "chore":
            errors.append("%s 是 L%d 但没有约束（至少写一条可判定的行为句）" % (e["id"], e["level"]))
        for c in e["constraints"]:
            if not c["id"].startswith(e["id"] + "."):
                errors.append("约束 %s 不属于 %s" % (c["id"], e["id"]))
            if len(c["text"]) < 6 or re.search(r"(处理好|注意|优化|合理|正确处理)", c["text"]):
                errors.append("约束 %s 太含糊，改成可判定的行为句：%s" % (c["id"], c["text"]))
        if e["type"] == "bug" and e["status"] == "settled" and f.get("根因", "待查").strip() in ("", "待查"):
            errors.append("%s 是已定稿的 bug，但「根因」还没写" % e["id"])
        for v in e["verified"]:
            if re.search(r"(应该|理论上|估计|看起来|大概|should|probably|seems)", v, re.I):
                errors.append("%s 的验证「%s」不是证据——写实际跑的命令和结果，没跑就标 ✗" % (e["id"], v))
        for s in e["supersedes"] + e["related"]:
            if s not in all_ids:
                errors.append("%s 引用了不存在的 %s" % (e["id"], s))
    if errors:
        print("✗ %d 个问题:" % len(errors))
        for x in errors:
            print("  - " + x)
        sys.exit(1)
    print("✓ 台账格式正常（%d 条）" % len(entries))


def cmd_backfill(a):
    root = repo_root()
    lp = ledger_path(root)
    if not os.path.exists(lp):
        return
    sha = git("rev-parse", "--short", a.commit).strip()
    files = git("diff-tree", "--no-commit-id", "--name-only", "-r", a.commit).split()
    entries, _ = parse_ledger(lp)
    targets = [e for e in entries if e["type"] != "baseline" and e["status"] != "dropped"
               and (e["status"] == "open" or not e["shas"])
               and any(path_match(f, e["files"]) for f in files) and sha not in e["shas"]]
    if not targets:
        return
    lines = open(lp, encoding="utf-8").read().splitlines()
    tid = {e["id"] for e in targets}
    settle = {e["id"] for e in targets if e["status"] == "open" and e["level"] is not None}
    out, cur, done = [], None, set()
    for i, ln in enumerate(lines):
        m = HEADER_RE.match(ln.strip()) if ln.startswith("### ") else None
        if m:
            cur = m.group(1)
            # 提交即定稿：已经出过卡片（级别已定）的 open 条目自动转 settled，用户不用管状态
            if cur in settle:
                ln = ln.replace(" · open", " · settled", 1)
        if cur in tid and cur not in done and FIELD_RE.match(ln):
            key = FIELD_RE.match(ln).group(1).strip()
            if key == "提交":
                val = FIELD_RE.match(ln).group(2).strip()
                val = "" if val.startswith(("(", "（", "待")) else val
                ln = "- 提交: %s" % (", ".join([x for x in [val, sha] if x]))
                done.add(cur)
        out.append(ln)
        # 没有「提交」字段：插在「文件」后面
        if cur in tid and cur not in done and FIELD_RE.match(ln) and FIELD_RE.match(ln).group(1).strip() in ("文件", "定稿"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not (FIELD_RE.match(nxt) and FIELD_RE.match(nxt).group(1).strip() == "提交"):
                out.append("- 提交: %s" % sha)
                done.add(cur)
    open(lp, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("ai-light-work: %s 已关联到 %s" % (sha, ", ".join(sorted(done))))


def cmd_stats(a):
    root = repo_root()
    entries, _ = parse_ledger(ledger_path(root))
    ch = [e for e in entries if e["type"] != "baseline"]
    if not ch:
        print("还没有变更条目。")
        return
    cnt = {i: 0 for i in range(4)}
    for e in ch:
        if e["level"] is not None:
            cnt[e["level"]] += 1
    n = sum(cnt.values()) or 1
    print("条目 %d · " % len(ch) + " · ".join("L%d %d" % (i, cnt[i]) for i in range(4)))
    by_type = {}
    for e in ch:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print("类型: " + " · ".join("%s %d" % kv for kv in by_type.items()))
    print("open %d · 未验证 %d 项" % (sum(e["status"] == "open" for e in ch), sum(len(e["unverified"]) for e in ch)))
    if cnt[3] / n > 0.3:
        print("⚠ L3 占比 %.0f%%：问题多半在改动粒度——先把需求拆小，而不是把 review 做得更重。" % (100.0 * cnt[3] / n))


def sym_of(text):
    m = DEF_RE.search(text or "")
    if m:
        return m.group(1) or m.group(2)
    m = re.search(r"([A-Za-z_]\w{2,})\s*\(", text or "")
    return m.group(1) if m else None


def blob_lines(root, tree, path):
    out = subprocess.run(["git", "show", "%s:%s" % (tree, path)], cwd=root,
                         capture_output=True, text=True, errors="replace")
    return out.stdout.splitlines() if out.returncode == 0 else []


def changed_symbols(root, frm, to, path):
    """本段改动落在哪些函数/类里：改动行本身是定义就用它，否则向上找最近的定义行。"""
    raw = git("diff", "-U0", "--no-color", frm, to, "--", path, cwd=root, check=False)
    old, new = None, None
    syms = set()
    o_ln = n_ln = 0
    for ln in raw.splitlines():
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$", ln)
        if m:
            o_ln, n_ln = int(m.group(1)), int(m.group(2))
            ctx_sym = sym_of(m.group(3))
            continue
        if ln.startswith(("+++", "---", "diff ", "index ")):
            continue
        if ln[:1] not in "+-":
            continue
        text = ln[1:]
        name = sym_of(text) if DEF_RE.search(text) else None
        if not name:
            if ln[0] == "-":
                old = old if old is not None else blob_lines(root, frm, path)
                src, at = old, o_ln
            else:
                new = new if new is not None else blob_lines(root, to, path)
                src, at = new, n_ln
            for i in range(min(at, len(src)) - 1, -1, -1):
                if DEF_RE.search(src[i]):
                    name = sym_of(src[i])
                    break
        syms.add(name or ctx_sym or "(文件级)")
        if ln[0] == "-":
            o_ln += 1
        else:
            n_ln += 1
    return syms


def cmd_conflicts(a):
    """/lw-check 用：多次改动之间的重叠、回退、反复修改。"""
    root = repo_root()
    cfg = load_cfg(root)
    lp = ledger_path(root)
    entries, _ = parse_ledger(lp)
    chain = checkpoint_chain()
    ids = None
    if a.recorded_only:
        ids = {e["id"] for e in change_entries(entries)}
        chain = [c for c in chain if c["entry"] in ids]
    by_id = {e["id"]: e for e in entries}
    test_re = re.compile(cfg["test_regex"])
    per = {}          # entry -> {"rounds", "keys", "added": {file: set}, "removed": {file: set}}
    order = []
    unowned = 0
    for c in chain:
        if not c["from"]:
            continue
        e = c["entry"] if c["entry"] and c["entry"] != "-" else None
        if e is None:
            unowned += 1
            e = "(未归属)"
        if e not in per:
            per[e] = {"rounds": 0, "keys": set(), "added": {}, "removed": {}}
            order.append(e)
        per[e]["rounds"] += 1
        for path, v in parse_diff(root, c["from"], c["tree"]).items():
            if glob_hit(path, cfg["exclude"]) or v["binary"] or test_re.search(path):
                continue
            for n in changed_symbols(root, c["from"], c["tree"], path):
                per[e]["keys"].add((path, n))
            for sign, text in v["lines"]:
                t = text.strip()
                if len(t) < 8 or t in ("}", "});", "end"):
                    continue
                per[e]["added" if sign == "+" else "removed"].setdefault(path, set()).add(t)
    # 热点：同一 文件+符号 被 ≥2 条改过
    hot = {}
    for e in order:
        for k in per[e]["keys"]:
            hot.setdefault(k, [])
            if e not in hot[k]:
                hot[k].append(e)
    hotspots = sorted([(k, v) for k, v in hot.items() if len(v) >= 2], key=lambda x: -len(x[1]))
    # 回退：后面的条目删掉了前面条目加入的行
    reverts = []
    for i, e1 in enumerate(order):
        for e2 in order[i + 1:]:
            if e1 == e2:
                continue
            for path, added in per[e1]["added"].items():
                gone = added & per[e2]["removed"].get(path, set())
                if gone:
                    reverts.append({"later": e2, "earlier": e1, "file": path, "n": len(gone),
                                    "sample": sorted(gone)[:2]})
    act, dead = active_constraints(entries)
    hot_files = sorted({k[0] for k, _ in hotspots})
    cons_on_hot = [c for c in act if c["global"] or any(path_match(f, c["files"]) for f in hot_files)]
    many_rounds = [(e, per[e]["rounds"]) for e in order if per[e]["rounds"] >= 3]
    sup = [(e["id"], e["supersedes"]) for e in entries
           if e["supersedes"] and (ids is None or e["id"] in ids)]
    # 未收尾
    pending = False
    if not a.recorded_only:
        segs, _ = resolve_range(root, cfg, "checkpoint")
        pending = git("rev-parse", segs[0][0] + "^{tree}").strip() != segs[0][1]

    res = {"entries_with_diff": order, "unowned_rounds": unowned,
           "hotspots": [{"file": k[0], "symbol": k[1], "entries": v} for k, v in hotspots],
           "reverts": reverts, "constraints_on_hotspots": [{"id": c["id"], "text": c["text"], "global": c["global"]} for c in cons_on_hot],
           "many_rounds": many_rounds, "supersedes": sup, "pending_changes": pending}
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    print("台账: %s · 有逐条 diff 的条目 %d 个（%s）" % (os.path.relpath(lp, root), len(order), ", ".join(order) or "-"))
    if not chain:
        print("还没有任何收尾点：逐条冲突分析要等 closeout 记录过至少一次。")
    if pending:
        print("⚠ 上次收尾点之后还有未收尾的改动（不属于任何条目）")
    if unowned:
        print("⚠ %d 轮改动没有归属条目（checkpoint 时没带 --entry）" % unowned)
    if reverts:
        print("\n回退/改写（后面的条目删掉了前面条目加入的代码——最常见的“改 A 坏 B”）:")
        for r in reverts:
            print("  - %s 删除/改写了 %s 在 %s 加入的 %d 行，例: %s" % (
                r["later"], r["earlier"], r["file"], r["n"], " ｜ ".join(x[:60] for x in r["sample"])))
    if hotspots:
        print("\n热点（同一处被多个条目改过）:")
        for h in res["hotspots"][:15]:
            print("  - %s · %s ← %s" % (h["file"], h["symbol"], " → ".join(h["entries"])))
    if many_rounds:
        print("\n反复修改（≥3 轮）: %s" % ", ".join("%s×%d" % x for x in many_rounds))
    if sup:
        print("\n需求更替链: %s" % "；".join("%s supersedes %s" % (i, ",".join(v)) for i, v in sup))
    if cons_on_hot:
        print("\n热点上需要逐条核对的有效约束:")
        for c in cons_on_hot:
            print("  - %s%s %s" % (c["id"], "[全局]" if c["global"] else "", c["text"]))
    if not (reverts or hotspots or many_rounds):
        print("\n没有发现条目之间的重叠或回退。")


# ───────────────────────── hooks（插件自动化）─────────────────────────

WORKFLOW = """[ai-light-work] 当前分支 {branch} 的变更台账：{ledger}。LW = python3 ~/.light-work/lw.py
结合上下文确认用户要求动手修改代码（修 bug / 需求变更 / 调整行为）后，按顺序自动执行：
  1. lw-record  先把这次意图记成 open 条目（同一意图的续改就更新原条目；推翻已有约束先问用户）
  2. lw-edit    bug 先找根因；按"手术刀"原则改代码；改动明显和台账约束冲突而用户没提时，先用一句话请用户选
  3. lw-review  用编辑前快照审阅本轮 → 定稿条目 → 输出 review 卡片 → 记录收尾点
按需调用 lw-check 核对回归和验证缺口。编辑流程调用 review 或 check 时传入本轮起点和条目号；用户直接调用时默认最近一条，支持 C-xxx、all、branch。纯问答、读代码、解释、跑命令不记账。
查 bug 想知道某行代码是哪次改动、为什么这么写：$LW why 文件:行。台账、状态、快照全部自动维护，不要让用户做任何设置。"""

CHANGE_INTENT = re.compile(
    r"(改|修|bug|报错|不对|不行|有问题|不要|换成|改成|加个|加上|新增|删掉|去掉|调整|优化|重构|实现|支持|兼容|"
    r"\bfix|\bchange|refactor|\badd\b|remove|update|implement)", re.I)


ERR_FILE = os.path.expanduser("~/.light-work/hook-error.txt")
PENDING_ERR = []   # 本次钩子要顺带告诉 AI 的上次错误
EMITTED = []


def note_error(where, e):
    """钩子不能把会话弄坏，但也不能一直默默失效：记下来，下次有机会时告诉用户一次。"""
    try:
        os.makedirs(os.path.dirname(ERR_FILE), exist_ok=True)
        with open(ERR_FILE, "w", encoding="utf-8") as f:
            f.write("%s %s: %s" % (_dt.datetime.now().strftime("%m-%d %H:%M"), where, e))
    except Exception:
        pass


def take_error():
    try:
        msg = open(ERR_FILE, encoding="utf-8").read().strip()
        os.remove(ERR_FILE)
    except Exception:
        return None
    return ("[ai-light-work] 插件上次运行出错（%s），台账可能没有正常记录。"
            "请在这一轮回复的开头用一句话告诉用户，并建议在 GitHub fahcode/ai-light-work 反馈。" % msg) if msg else None


def emit(event, text=None, extra=None):
    out = dict(extra or {})
    if PENDING_ERR:
        text = "\n".join([PENDING_ERR.pop()] + ([text] if text else []))
    EMITTED.append(event)
    if text:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": text}
    if out:
        print(json.dumps(out, ensure_ascii=False))


def install_shim():
    """把当前脚本复制到稳定路径，技能和 git 钩子都用 ~/.light-work/lw.py。"""
    me = os.path.abspath(__file__)
    dst = os.path.expanduser("~/.light-work/lw.py")
    try:
        if os.path.abspath(dst) == me:
            return
        if not os.path.exists(dst) or open(dst, "rb").read() != open(me, "rb").read():
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = "%s.%d.tmp" % (dst, os.getpid())
            shutil.copyfile(me, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dst)  # 原子替换：另一个会话（或另一个工具里的版本）同时在读也不会读到半个文件
    except Exception as e:
        note_error("复制 ~/.light-work/lw.py", e)


def ledger_branch_ctx(root):
    """返回当前 Git 工作区的台账上下文。"""
    cfg = load_cfg(root)
    lp = ledger_path(root)
    if not os.path.exists(lp):  # 会话中途切了分支：当场给新分支建台账，用户无感
        lp, _ = init_ledger(root, cfg, quiet=True)
    return cfg, lp


def capture(fn, *args):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def rel_to_root(root, path):
    if not path:
        return None
    ap = os.path.realpath(path)  # git 给的 root 已解析符号链接，工具给的路径可能没解析
    try:
        rp = os.path.relpath(ap, os.path.realpath(root))
    except ValueError:
        return None
    return None if rp.startswith("..") else rp.replace(os.sep, "/")


# Codex 的文件编辑都走 apply_patch：tool_input 里没有 file_path，路径写在补丁文本的这几种行里
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to):\s*(.+?)\s*$", re.M)


def edited_paths(root, data):
    """这次工具调用要改哪些文件（仓库相对路径，不含台账目录）。
    Claude Code / CodeBuddy：tool_input.file_path；Codex：tool_name=apply_patch，从补丁文本里解析。"""
    ti = data.get("tool_input")
    cands = []
    if isinstance(ti, dict):
        cands = [ti[k] for k in ("file_path", "notebook_path", "path") if isinstance(ti.get(k), str) and ti[k]]
    if not cands:  # 补丁字段名各家不一样（patch / input / command），所有字符串都扫一遍最稳
        texts = []

        def walk(v):
            if isinstance(v, str):
                texts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(ti)
        for t in texts:
            cands += PATCH_FILE_RE.findall(t)
    base = data.get("cwd") or os.getcwd()
    out = []
    for p in cands:
        f = rel_to_root(root, p if os.path.isabs(p) else os.path.join(base, p))
        if f and not f.startswith(LEDGER_DIR + "/") and f not in out:
            out.append(f)
    return out


def hook_session_start(root, data):
    install_shim()
    cfg, lp = ledger_branch_ctx(root)
    lp, created = init_ledger(root, cfg, quiet=True)
    text = WORKFLOW.format(branch=current_branch(), ledger=os.path.relpath(lp, root))
    ns = argparse.Namespace(files=None, limit=12)
    body = capture(cmd_context, ns).strip()
    emit("SessionStart", text + "\n\n" + body[:3500])


def hook_prompt(root, data):
    _, lp = ledger_branch_ctx(root)
    st = state_load(root)
    if st.get("awaiting"):  # 用户回复了冲突确认，恢复正常的收尾检查
        st["awaiting"] = False
        state_save(root, st)
    entries, _ = parse_ledger(lp)
    opens = [e for e in entries if e["status"] == "open" and e["type"] != "baseline"]
    parts = []
    if opens:
        parts.append("open 条目: " + "；".join("%s %s（第 %d 轮）" % (e["id"], e["title"], max(e["rounds"], 1))
                                             for e in opens[-3:]))
    stuck = [e for e in opens if e["rounds"] >= 3]
    if stuck:
        parts.append("⚠ %s 已经改到第 %d 轮：如果这次还是在修同一个问题，先停下来重新找根因（多半是修错了地方），"
                     "不要继续叠补丁，并在卡片里告诉用户换了方向。" % (stuck[-1]["id"], stuck[-1]["rounds"]))
    if CHANGE_INTENT.search(data.get("prompt", "") or ""):
        parts.append("请结合上下文判断用户是否要求动手修改代码。纯提问、分析或解释就直接回答，不记条目。"
                     "确认需要动手时，先 lw-record（同一意图续改就更新原条目），再 lw-edit，最后 lw-review。")
    if parts:
        emit("UserPromptSubmit", "[ai-light-work] " + " ".join(parts))


def hook_pre_edit(root, data):
    _, lp = ledger_branch_ctx(root)
    files = edited_paths(root, data)
    if not files:
        return
    st = state_load(root)
    entries, _ = parse_ledger(lp)
    act, _ = active_constraints(entries)
    msgs = []
    shown = st.get("ctx_shown")
    if not isinstance(shown, dict):
        shown = {}
    glb = [c for c in act if c["global"]]
    new_glb = [c for c in glb if c["id"] not in shown.get("*", [])]
    for f in files:  # Codex 一个补丁可能同时改好几个文件
        rel = [c for c in act if not c["global"] and path_match(f, c["files"])]
        new_rel = [c for c in rel if c["id"] not in shown.get(f, [])]
        if new_rel or new_glb:
            msgs.append("改 %s 前必须保持的约束：" % f)
            msgs += ["- %s%s %s" % (c["id"], "[全局]" if c["global"] else "", c["text"]) for c in (new_glb + new_rel)[:12]]
            new_glb = []
        shown[f] = sorted(set(shown.get(f, [])) | {c["id"] for c in rel})
    if msgs:
        msgs.append("这次改动如果会让其中某条明确不成立、而用户没提到，先按 lw-edit 的格式问一句再改。")
    shown["*"] = sorted(set(shown.get("*", [])) | {c["id"] for c in glb})
    st["ctx_shown"] = shown
    if not [e for e in entries if e["status"] == "open" and e["type"] != "baseline"]:
        msgs.append("还没有 open 条目：先按 lw-record 记下这次修改的意图，再改代码。")
    state_save(root, st)
    if msgs:
        emit("PreToolUse", "[ai-light-work] " + "\n".join(msgs))


def hook_post_edit(root, data):
    ledger_branch_ctx(root)
    files = edited_paths(root, data)
    if not files:
        return
    st = state_load(root)
    st["dirty"] = True
    state_save(root, st)


def hook_stop(root, data):
    if data.get("stop_hook_active"):
        return
    cfg, _ = ledger_branch_ctx(root)
    st = state_load(root)
    if not st.get("dirty") or st.get("awaiting"):  # 没改代码，或正在等用户回答冲突确认
        return
    segs, _ = resolve_range(root, cfg, "checkpoint")
    base, cur = segs[0]
    if git("rev-parse", base + "^{tree}").strip() == cur:
        state_clear(root)
        return
    if st.get("nag_tree") == cur:
        return
    st["nag_tree"] = cur
    state_save(root, st)
    emit("Stop", extra={"systemMessage": "[ai-light-work] 改动尚未收尾，需要时可以让助手补做 lw-review。"})


def cmd_why(a):
    """文件:行 → git blame → 提交号 → 台账条目。未提交的行按文件归到 open 条目。"""
    root = repo_root()
    entries, _ = parse_ledger(ledger_path(root))
    entries = [e for e in entries if e["status"] != "dropped"]
    _, dead = active_constraints(entries)
    later = {}
    for e in entries:
        for s_ in e["supersedes"]:
            later.setdefault(s_.split(".")[0], []).append(e["id"])
    cfg = load_cfg(root)
    mb = merge_base(detect_base(cfg))
    sr = start_ref()
    start_ct = int(git("log", "-1", "--format=%ct", sr, check=False).strip() or 0) if git_ok("rev-parse", "--verify", "-q", sr) else 0

    def show(e, path):
        print("  %s · %s · %s · %s" % (e["id"], e["type"], e["status"], e["title"]))
        f = e["fields"]
        for k in ("意图", "根因"):
            if f.get(k) and f[k] not in ("待查", "-", "无"):
                print("    %s: %s" % (k, f[k]))
        for c in e["constraints"]:
            print("    %s %s%s" % (c["id"], c["text"], "（已作废）" if c["id"] in dead else ""))
        if later.get(e["id"]):
            print("    后来被推翻/改写: %s" % ", ".join(later[e["id"]]))

    for spec in a.targets:
        m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", spec)
        if not m:
            die("格式：文件:行号 或 文件:起-止，例如 src/a.ts:42")
        path = rel_to_root(root, m.group(1)) or m.group(1)
        lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
        raw = git("blame", "--porcelain", "-L", "%d,%d" % (lo, hi), "--", path, check=False, cwd=root)
        shas = []
        for ln in raw.splitlines():
            mm = re.match(r"^([0-9a-f]{40}) \d+ \d+", ln)
            if mm and mm.group(1) not in shas:
                shas.append(mm.group(1))
        if not shas:
            print("%s: git blame 没有结果（文件不存在或行号超出范围）" % spec)
            continue
        by_file = [e for e in entries if e["type"] != "baseline" and path_match(path, e["files"])]
        for sha in shas:
            if set(sha) == {"0"}:
                print("%s ← 还没提交的改动" % spec)
                hits = [e for e in by_file if e["status"] == "open"] or by_file[-1:]
                for e in hits:
                    show(e, path)
                if not hits:
                    print("  台账里没有条目登记过这个文件（可能是没走台账的改动）")
                continue
            info = git("log", "-1", "--date=short", "--format=%h %ad %s", sha, check=False, cwd=root).strip()
            print("%s ← %s" % (spec, info))
            hits = [e for e in entries if any(sha.startswith(x) for x in e["shas"])]
            narrowed = [e for e in hits if path_match(path, e["files"])]
            if narrowed or hits:
                for e in (narrowed or hits):
                    show(e, path)
                continue
            if mb and git_ok("merge-base", "--is-ancestor", sha, mb):
                print("  基准分支上就有的代码，不是这个分支改的")
            elif start_ct and int(git("log", "-1", "--format=%ct", sha, check=False).strip() or 0) <= start_ct:
                print("  台账起点之前的提交（最初的功能开发），台账里没有逐条记录")
            else:
                print("  这个提交没有对应的台账条目（可能是没走台账的改动）")
                for e in by_file[-3:]:
                    print("  同文件的条目: %s %s" % (e["id"], e["title"]))


def cmd_ask(a):
    """lw-edit 冲突闸门：提问前调用，这一轮结束时跳过收尾提醒；用户下一条消息到来时自动解除。"""
    root = repo_root()
    st = state_load(root)
    st["awaiting"] = True
    state_save(root, st)
    print("✓ 已标记为等待用户确认")


def cmd_hook(a):
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    ev = {"session-start": "SessionStart", "prompt": "UserPromptSubmit"}.get(a.event)
    try:
        if data.get("cwd") and os.path.isdir(data["cwd"]):
            os.chdir(data["cwd"])
        root = git("rev-parse", "--show-toplevel", check=False).strip()
        if not root:
            return
        if ev:
            err = take_error()
            if err:
                PENDING_ERR.append(err)
        {"session-start": hook_session_start, "prompt": hook_prompt, "pre-edit": hook_pre_edit,
         "post-edit": hook_post_edit, "stop": hook_stop}[a.event](root, data)
    except SystemExit:
        pass
    except Exception as e:  # 钩子永远不能把会话弄坏：不报错退出，记下来下次告诉用户
        if os.environ.get("LW_DEBUG"):
            raise
        sys.stderr.write("ai-light-work hook %s: %s\n" % (a.event, e))
        note_error("钩子 " + a.event, e)
    if PENDING_ERR and not EMITTED:
        emit(ev)
    sys.exit(0)


VIEW_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  color-scheme: light dark;
  --background: #f4f6f9; --panel: #fff; --text: #182431; --muted: #48586b;
  --border: #d8dfe8; --link: #1756a5; --accent: #e8f1ff;
  --add: #e6f6ed; --del: #fce9eb;
  --flow-ink: #124d85; --flow-wash: #eef6ff; --flow-rule: #a4c5e7;
  --sequence-ink: #593995; --sequence-wash: #f5f0ff; --sequence-rule: #c7b5e9;
  --architecture-ink: #166653; --architecture-wash: #eaf7f3; --architecture-rule: #9acbbd;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --background: #10151d; --panel: #18212d; --text: #e9eff6; --muted: #aabaca;
    --border: #354454; --link: #9ac5ff; --accent: #203553;
    --add: #183b30; --del: #472731;
    --flow-ink: #a4ceff; --flow-wash: #1d3149; --flow-rule: #4b739c;
    --sequence-ink: #d7c3ff; --sequence-wash: #33274c; --sequence-rule: #78639c;
    --architecture-ink: #abebd9; --architecture-wash: #193a33; --architecture-rule: #4e8d7e;
  }}
}}
* {{ box-sizing: border-box }}
body {{ margin: 0; background: var(--background); color: var(--text);
  font: 1rem/1.6 Geist, system-ui, sans-serif }}
.layout {{ max-width: 92rem; margin: auto; padding: 2rem; display: grid;
  grid-template-columns: minmax(0, 15rem) minmax(0, 1fr); gap: 2rem }}
nav {{ position: sticky; top: 2rem; max-height: calc(100vh - 4rem);
  overflow: auto; align-self: start; padding: .75rem; background: var(--panel);
  border: 1px solid var(--border); border-radius: .75rem }}
nav a {{ display: block; padding: .375rem .5rem; overflow-wrap: anywhere;
  border-radius: .375rem; text-decoration: none; font-size: .875rem }}
nav a:not(.nav-file) {{ font-weight: 600 }}
nav .nav-file {{ color: var(--text) }}
nav .nav-file strong, nav .nav-file small {{ display: block; line-height: 1.35 }}
nav .nav-file small {{ margin-top: .125rem; font-size: .75rem }}
a {{ color: var(--link); text-underline-offset: .2em }}
a:hover {{ background: var(--accent) }}
a:focus-visible, summary:focus-visible {{ outline: 2px solid var(--link); outline-offset: 2px }}
main {{ min-width: 0 }}
header, .panel {{ background: var(--panel); border: 1px solid var(--border);
  border-radius: .75rem; padding: clamp(1rem, 3vw, 2rem); margin-bottom: 1rem }}
header {{ border-block-start: .25rem solid var(--link) }}
h1, h2, h3 {{ line-height: 1.25; text-wrap: balance }}
h1 {{ font-size: 1.875rem; margin: .25rem 0 .75rem }}
h2 {{ font-size: 1.25rem; margin: 0 0 .75rem }}
h3 {{ font-size: 1.125rem }}
p, li {{ text-wrap: pretty }}
.meta, small {{ color: var(--muted) }}
.meta {{ overflow-wrap: anywhere }}
.facts {{ display: flex; gap: .5rem 1.25rem; flex-wrap: wrap; padding: 0; margin: 1rem 0 0 }}
.facts li {{ list-style: none; font-weight: 600; padding: .125rem .625rem;
  border: 1px solid var(--border); border-radius: .375rem; font-variant-numeric: tabular-nums }}
.overview dl {{ margin: 1rem 0 0; display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: .375rem 1rem }}
dt {{ font-weight: 600 }} dd {{ margin: 0; overflow-wrap: anywhere }}
.report > :first-child {{ margin-top: 0 }}
blockquote {{ margin: .75rem 0; padding: .5rem 1rem; background: var(--accent);
  border-inline-start: .25rem solid var(--link) }}
code, pre, .numbers {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace }}
pre {{ margin: 0; overflow: auto; white-space: pre; tab-size: 4 }}
.report pre, #ledger-raw pre {{ background: var(--background); padding: .75rem }}
.table {{ overflow: auto }} table {{ width: 100%; border-collapse: collapse }}
th, td {{ text-align: left; padding: .5rem .75rem; border: 1px solid var(--border) }}
summary {{ cursor: pointer; font-weight: 600; overflow-wrap: anywhere }}
.file {{ margin: .75rem 0; border: 1px solid var(--border); border-radius: .5rem }}
.file summary {{ padding: .75rem; background: var(--background) }}
.file > p {{ padding-inline: .75rem }}
.file .meta {{ font-weight: 400; margin-inline-start: .75rem }}
.hunk {{ scroll-margin-top: 1rem; padding-block: .5rem }}
.hunk + .hunk {{ border-top: 1px solid var(--border) }}
.hunk header {{ font: .875rem/1.5 ui-monospace, monospace; margin: 0; border: 0;
  padding: .375rem .75rem; background: var(--accent); overflow-wrap: anywhere }}
.hunk pre {{ font-size: .875rem; line-height: 1.5 }}
.line {{ display: block; min-width: max-content; padding-inline-end: 1rem }}
.line.add {{ background: var(--add) }} .line.del {{ background: var(--del) }}
.line {{ scroll-margin-top: 1rem }}
.omitted {{ padding: .375rem .75rem; color: var(--muted); background: var(--background) }}
.numbers {{ display: inline-block; min-width: 12ch; padding-inline: .5rem;
  color: var(--muted); user-select: none }}
:target {{ outline: 2px solid var(--link); outline-offset: 2px }}
.flow {{ display: flex; gap: 1rem; flex-wrap: wrap; padding: 0; list-style: none;
  counter-reset: step }}
.flow li {{ padding: .5rem .75rem; border-inline-start: .25rem solid var(--link);
  background: var(--accent); max-width: 22rem; counter-increment: step }}
.flow li::before {{ content: counter(step) ". "; color: var(--link); font-weight: 600 }}
.diagram {{ --diagram-ink: var(--flow-ink); --diagram-wash: var(--flow-wash); --diagram-rule: var(--flow-rule);
  margin: 1rem 0; padding: 1rem; border: 1px solid var(--diagram-rule);
  border-radius: .75rem; background: var(--diagram-wash) }}
.diagram.sequence {{ --diagram-ink: var(--sequence-ink); --diagram-wash: var(--sequence-wash);
  --diagram-rule: var(--sequence-rule) }}
.diagram.architecture {{ --diagram-ink: var(--architecture-ink); --diagram-wash: var(--architecture-wash);
  --diagram-rule: var(--architecture-rule) }}
.diagram figcaption {{ margin-bottom: .875rem; padding-bottom: .5rem;
  color: var(--diagram-ink); border-bottom: 1px solid var(--diagram-rule); font-weight: 600 }}
.diagram ol, .diagram ul {{ display: grid; gap: .5rem; margin: 0; padding: 0; list-style: none }}
.diagram .edge {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr) minmax(0, 1fr);
  gap: .5rem; align-items: center; overflow-wrap: anywhere }}
.diagram .node {{ background: var(--panel); border: 1px solid var(--diagram-rule);
  border-radius: .5rem; padding: .375rem .625rem; text-align: center; font-weight: 600 }}
.diagram .connector {{ color: var(--diagram-ink); text-align: center; font-size: .875rem }}
.diagram .arrow {{ display: flex; align-items: center; gap: .25rem;
  line-height: 1; font-size: 1.25rem }}
.diagram .arrow::before {{ content: ""; flex: 1; border-top: 1px solid currentColor }}
.sequence ol {{ counter-reset: message }}
.sequence .edge {{ counter-increment: message }}
.sequence .connector::before {{ content: counter(message) ". "; font-weight: 600 }}
.architecture .groups {{ gap: .75rem }}
.architecture .group {{ border: 1px solid var(--diagram-rule); background: var(--panel);
  border-radius: .5rem; padding: .75rem }}
.architecture h3 {{ margin: 0 0 .5rem; color: var(--diagram-ink) }}
.architecture .edge {{ grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) }}
@media (max-width: 48rem) {{
  .layout {{ display: block; padding: .75rem }}
  nav {{ position: static; max-height: 10rem; margin-bottom: .75rem; border: 1px solid var(--border) }}
}}
</style></head>
<body><div class="layout">
<nav aria-label="报告导航">{nav}</nav>
<main>
<header class="overview"><div class="meta">{scope}</div><h1>{title}</h1>{facts}{overview}</header>
{report}
{evidence}
<details class="panel" id="ledger-raw"><summary>台账原文</summary><pre>{ledger}</pre></details>
</main></div>
<script>
function reveal() {{
  const target = document.getElementById(location.hash.slice(1));
  if (!target) return;
  const details = target.closest('details');
  if (details) details.open = true;
  target.scrollIntoView();
}}
window.addEventListener('hashchange', reveal);
document.addEventListener('click', event => {{
  const link = event.target.closest('a');
  if (link && link.hash && link.hash === location.hash) reveal();
}});
reveal();
</script>
</body></html>
"""


def diff_rows(hunk):
    old, new = hunk["old_start"], hunk["new_start"]
    for text in hunk["body"]:
        kind = {"-": "del", "+": "add"}.get(text[:1], "")
        if text.startswith("\\"):
            yield "", None, None, text
            continue
        old_line = old if kind != "add" else None
        new_line = new if kind != "del" else None
        yield kind, old_line, new_line, text
        old += old_line is not None
        new += new_line is not None


def report_references(report):
    references = {}
    for path, line in re.findall(r"`([^`\n]+?):(\d+)(?:-\d+)?`", report):
        references.setdefault(path, set()).add(int(line))
    return references


def patch_excerpt(hunk, references, limit=40):
    rows = list(diff_rows(hunk))
    if len(rows) <= limit:
        return list(enumerate(rows, 1))
    matches = [i for i, (_, old, new, _) in enumerate(rows) if old in references or new in references]
    selected = set(matches[:limit])
    # 先保留引用及左右文，再补片段起始处。
    for indexes in ([i + distance for i in matches for distance in (-1, 1, -2, 2)], range(limit)):
        for i in indexes:
            if len(selected) == limit:
                break
            if 0 <= i < len(rows):
                selected.add(i)
    return [(i + 1, rows[i]) for i in sorted(selected)]


def render_evidence(files, skipped, references):
    nav, panels, locations = [], [], {}
    for i, (path, diff) in enumerate(sorted(files.items()), 1):
        file_id = "file-%d" % i
        label = escape(path)
        folder, _, name = path.rpartition("/")
        directory = f"<small>{escape(folder)}</small>" if folder else ""
        nav.append(f'<a class="nav-file" href="#{file_id}" aria-label="{label}">'
                   f'<strong>{escape(name)}</strong>{directory}</a>')
        locations[path] = (file_id, {})
        counts = "二进制文件" if diff["binary"] else "+%d / −%d" % (diff["add"], diff["del"])
        if path in skipped:
            panels.append(f'<details class="file" id="{file_id}"><summary><code>{label}</code>'
                          f'<span class="meta">{counts}</span></summary><p>已略去补丁，仅保留文件统计。</p></details>')
            continue
        cited = references.get(path, set())
        indexed = list(enumerate(diff["hunks"], 1))
        def has_reference(item):
            hunk = item[1]
            return any(hunk[side + "_start"] <= line < hunk[side + "_start"] + hunk[side + "_len"]
                       for line in cited for side in ("new", "old"))
        selected = sorted(sorted(indexed, key=lambda item: not has_reference(item))[:6], key=lambda item: item[0])
        hunks = []
        for j, hunk in selected:
            hunk_id = "%s-hunk-%d" % (file_id, j)
            rows = []
            preceding = 0
            for n, (kind, old, new, text) in patch_excerpt(hunk, cited):
                if n > preceding + 1:
                    rows.append(f'<span class="line omitted">省略 {n - preceding - 1} 行</span>')
                preceding = n
                row_id = "%s-line-%d" % (hunk_id, n)
                if old is not None:
                    locations[path][1].setdefault(old, []).append((False, j, row_id))
                if new is not None:
                    locations[path][1].setdefault(new, []).append((True, j, row_id))
                numbers = "%s %s" % (str(old or "").rjust(5), str(new or "").rjust(5))
                tail = '<span class="meta"> …（该行已截断）</span>' if len(text) > 600 else ""
                rows.append(f'<span class="line {kind}" id="{row_id}"><span class="numbers">{numbers}</span>{escape(text[:600])}{tail}</span>')
            if preceding < len(hunk["body"]):
                rows.append(f'<span class="line omitted">省略后续 {len(hunk["body"]) - preceding} 行</span>')
            body = "".join(rows)
            header = escape(hunk["revision"] + "  " + hunk["header"])
            hunks.append(f'<section class="hunk" id="{hunk_id}"><header>{header}</header><pre aria-label="旧行、新行和补丁">{body}</pre></section>')
        body = "".join(hunks) or "<p>无文本补丁。</p>"
        if len(selected) < len(indexed):
            body += f'<p class="meta">另有 {len(indexed) - len(selected)} 个补丁片段，完整差异可在 Git 中查看。</p>'
        panels.append(f'<details class="file" id="{file_id}"><summary><code>{label}</code>'
                      f'<span class="meta">{counts}</span></summary>{body}</details>')
    return "".join(nav), "".join(panels), locations


def report_inline(text, locations):
    parts = re.split(r'(`[^`\n]+`|\*\*[^*\n]+\*\*)', text)
    for i, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            value = part[1:-1]
            code = "<code>" + escape(value) + "</code>"
            match = re.fullmatch(r"(.+?):(\d+)(?:-\d+)?", value)
            path = match[1] if match else value
            if path in locations:
                file_id, lines = locations[path]
                candidates = lines.get(int(match[2]), []) if match else []
                # 选择新侧行号优先，历史有重叠时定位到最后一轮。
                target = max(candidates)[2] if candidates else file_id
                code = f'<a href="#{target}">{code}</a>'
            parts[i] = code
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            parts[i] = "<strong>" + escape(part[2:-2]) + "</strong>"
        else:
            parts[i] = escape(part)
    return "".join(parts)


def render_diagram(kind, source, inline):
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if kind == "flow" and not any(" -> " in line for line in lines):
        return '<ol class="flow">' + "".join("<li>" + inline(line) + "</li>" for line in lines) + "</ol>"
    edges = []
    for line in lines:
        relation, _, label = line.partition(" | ")
        origin, arrow, target = relation.partition(" -> ")
        if not arrow or not origin.strip() or not target.strip():
            return "<pre><code>" + escape(source) + "</code></pre>"
        edges.append((origin.strip(), target.strip(), label.strip()))
    if not edges:
        return ""

    def edge(origin, target, label):
        start = '<span class="node">' + inline(origin) + '</span>' if origin else ""
        direction = '<span class="connector">' + inline(label) + '<span class="arrow" aria-label="指向">→</span></span>'
        return '<li class="edge">' + start + direction + '<span class="node">' + inline(target) + '</span></li>'

    if kind == "architecture":
        groups = {}
        for origin, target, label in edges:
            groups.setdefault(origin, []).append(edge("", target, label))
        body = '<ul class="groups">' + "".join(
            '<li class="group"><h3>' + inline(origin) + '</h3><ul>' + "".join(items) + '</ul></li>'
            for origin, items in groups.items()) + '</ul>'
    else:
        tag = "ol" if kind == "sequence" else "ul"
        body = '<' + tag + '>' + "".join(edge(*item) for item in edges) + '</' + tag + '>'
    caption = {"flow": "流程图", "sequence": "交互图", "architecture": "架构图"}[kind]
    variant = "flowchart" if kind == "flow" else kind
    return f'<figure class="diagram {variant}"><figcaption>{caption}</figcaption>{body}</figure>'


def render_report(report, locations):
    """渲染报告使用的 Markdown 子集，原始 HTML 一律作为文本。"""
    def inline(text):
        return report_inline(text, locations)

    blocks = []
    for i, part in enumerate(re.split(r"(?m)^```([^\n]*)\n(.*?)^```[ \t]*$", report, flags=re.S), 0):
        if i % 3 == 1:
            language = part.strip()
        elif i % 3 == 2:
            if language in ("flow", "sequence", "architecture"):
                blocks.append(render_diagram(language, part, inline))
            elif language == "mermaid":
                blocks.append("<details><summary>Mermaid 流程源文本</summary><pre>" + escape(part) + "</pre></details>")
            elif language == "diff":
                rows = []
                for line in part.splitlines():
                    kind = {"+": "add", "-": "del"}.get(line[:1], "")
                    rows.append(f'<span class="line {kind}">{escape(line)}</span>')
                blocks.append("<pre><code>" + "".join(rows) + "</code></pre>")
            else:
                blocks.append("<pre><code>" + escape(part) + "</code></pre>")
        else:
            for paragraph in re.split(r"\n[ \t]*\n", part.strip()):
                lines = paragraph.splitlines()
                if not lines:
                    continue
                heading = re.match(r"^#{1,6} (.+)$", lines[0])
                if heading:
                    blocks.append("<h2>" + inline(heading[1]) + "</h2>")
                    lines = lines[1:]
                if not lines:
                    continue
                if all(line.startswith(">") for line in lines):
                    blocks.append("<blockquote>" + "<br>".join(inline(line[1:].strip()) for line in lines) + "</blockquote>")
                elif all(re.match(r"^(?:[-*]|\d+\.) ", line) for line in lines):
                    tag = "ol" if lines[0][0].isdigit() else "ul"
                    items = "".join("<li>" + inline(line.split(" ", 1)[1]) + "</li>" for line in lines)
                    blocks.append("<" + tag + ">" + items + "</" + tag + ">")
                elif len(lines) > 1 and all(line.startswith("|") for line in lines) and re.fullmatch(r"[|:\- ]+", lines[1]):
                    table = []
                    for n, line in enumerate(lines[:1] + lines[2:]):
                        tag = "th" if n == 0 else "td"
                        cells = "".join(f"<{tag}>{inline(cell.strip())}</{tag}>" for cell in line.strip("|").split("|"))
                        table.append("<tr>" + cells + "</tr>")
                    blocks.append('<div class="table"><table>' + "".join(table) + "</table></div>")
                else:
                    blocks.append("<p>" + "<br>".join(inline(line) for line in lines) + "</p>")
    return "\n".join(blocks)


def review_page(title, branch, report, ledger, analysis):
    files = analysis["evidence"] if analysis else {}
    skipped = set(analysis["skipped"]) if analysis else set()
    nav, panels, locations = render_evidence(files, skipped, report_references(report))
    facts = overview = ""
    scope = branch
    if analysis:
        scope += " · " + analysis["range"]
        added = sum(file_diff["add"] for file_diff in files.values())
        deleted = sum(file_diff["del"] for file_diff in files.values())
        facts = '<ul class="facts"><li>L%d</li><li>%d 个文件</li><li>+%d / −%d</li></ul>' % (
            analysis["level"], len(files), added, deleted)
        if len(analysis["entries"]) == 1:
            fields = analysis["entries"][0]["fields"]
            values = [f"<dt>{key}</dt><dd>{escape(fields[key])}</dd>" for key in ("意图", "验证") if fields.get(key)]
            if values:
                overview = "<dl>" + "".join(values) + "</dl>"
    if report:
        nav = '<a href="#report">审阅结论</a>' + nav
        report = '<section class="panel report" id="report">' + render_report(report, locations) + "</section>"
    evidence = ""
    if analysis:
        nav += '<a href="#evidence">全部代码证据</a>'
        evidence = '<section class="panel" id="evidence"><h2>代码证据</h2>' + (
            panels or "<p>这个范围没有文件差异。</p>") + "</section>"
    nav += '<a href="#ledger-raw">台账原文</a>'
    return VIEW_HTML.format(title=escape(title), scope=escape(scope), facts=facts, overview=overview,
                            nav=nav, report=report, evidence=evidence, ledger=escape(ledger))


def cmd_view(a):
    """生成可离线阅读的 HTML，指定审阅范围时内嵌 Git diff。"""
    root = repo_root()
    br = current_branch()
    lp = ledger_path(root, br)
    if not os.path.exists(lp):
        die("当前分支还没有台账")
    report = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    analysis = None
    if a.scope or a.base:
        mode, ref = pick_mode(a, "entry")
        analysis = analyze(root, load_cfg(root), mode, ref, 0, a.current_entry, evidence=True)
    out = page_path(root, br)
    with open(lp, encoding="utf-8") as ledger:
        page = review_page(a.title or "改动审阅", br, report, ledger.read(), analysis)
    with open(out, "w", encoding="utf-8") as output:
        output.write(page)
    print("✓ %s" % os.path.relpath(out, root))
    if a.open:
        opener = "open" if sys.platform == "darwin" else ("start" if os.name == "nt" else "xdg-open")
        try:
            subprocess.run([opener, out], shell=(os.name == "nt"))
        except Exception:
            print("  （无法自动打开，请手动打开上面的文件）")


def main():
    ap = argparse.ArgumentParser(prog="lw.py", description="ai-light-work 按分支变更台账")
    sp = ap.add_subparsers(dest="cmd")
    p = sp.add_parser("init"); p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_init)
    p = sp.add_parser("context"); p.add_argument("--files", nargs="*"); p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_context)
    p = sp.add_parser("next-id"); p.add_argument("--baseline", action="store_true"); p.set_defaults(fn=cmd_next_id)
    for name, fn, nh in (("review", cmd_score, 4), ("check", cmd_check, 0), ("score", cmd_score, 4)):
        p = sp.add_parser(name)
        g = p.add_mutually_exclusive_group()
        if name != "score":
            g.add_argument("scope", nargs="?", type=parse_scope, metavar="[C-xxx | all | branch]",
                           help="默认最近一条；全部用 all/a，全分支用 branch/b")
        g.add_argument("--base", help="从指定 ref 到工作区")
        p.add_argument("--current-entry", help="与 --base 一起标记编辑流程的本轮条目")
        g.add_argument("--branch", action="store_true", help="整个分支（分叉点..工作区）")
        g.add_argument("--ledger", action="store_true", help="台账范围（台账起点..工作区）")
        g.add_argument("--since-checkpoint", action="store_true", help="上次收尾点..工作区")
        g.add_argument("--entry", help="某个条目所有轮次的改动，如 C-004")
        if name == "score":
            g.add_argument("--latest-entry", action="store_true", help="最近一条台账改动")
        else:
            g.add_argument("--all-entries", action="store_true", help="所有台账条目已收尾的改动")
        p.add_argument("--hunks", type=int, default=nh)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
    sp.add_parser("snapshot", help="获取本轮编辑前的工作区 tree").set_defaults(fn=cmd_snapshot)
    p = sp.add_parser("checkpoint"); p.add_argument("--entry"); p.add_argument("--base"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_checkpoint)
    p = sp.add_parser("conflicts"); p.add_argument("--json", action="store_true")
    p.add_argument("--recorded-only", action="store_true", help="只分析已归属台账条目的收尾点")
    p.set_defaults(fn=cmd_conflicts)
    sp.add_parser("lint").set_defaults(fn=cmd_lint)
    sp.add_parser("ask", help="冲突闸门提问前调用").set_defaults(fn=cmd_ask)
    p = sp.add_parser("why", help="这一行是哪条台账改动引入的"); p.add_argument("targets", nargs="+", metavar="文件:行")
    p.set_defaults(fn=cmd_why)
    p = sp.add_parser("backfill"); p.add_argument("--commit", default="HEAD"); p.set_defaults(fn=cmd_backfill)
    sp.add_parser("stats").set_defaults(fn=cmd_stats)
    p = sp.add_parser("view"); p.add_argument("--open", action="store_true")
    p.add_argument("--title", help="页面标题，如：分支汇总 / C-004 review"); p.set_defaults(fn=cmd_view)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scope", type=parse_view_scope, metavar="SCOPE", help="报告的审阅范围：latest、C-xxx、all 或 branch")
    g.add_argument("--base", help="本轮编辑前的起点")
    p.add_argument("--current-entry", help="与 --base 一起标记本轮条目")
    p = sp.add_parser("hook"); p.add_argument("event", choices=["session-start", "prompt", "pre-edit", "post-edit", "stop"])
    p.set_defaults(fn=cmd_hook)
    a = ap.parse_args()
    if getattr(a, "current_entry", None) and not a.base:
        ap.error("--current-entry 需要同时提供 --base")
    if not getattr(a, "fn", None):
        ap.print_help()
        return
    try:
        a.fn(a)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
