#!/usr/bin/env python3
"""Light Work: branch-local change records and evidence-based reviews.

Requires Python 3.8+ and Git. The CLI computes scope, changed files, constraint
matches and reference hints. The assistant records intent and judges behavior.

Commands used by skills:
  context [--files PATH ...]    Show active constraints.
  next-id [--baseline]         Allocate the next change ID.
  review [C-xxx | all | branch]   Inspect changes and evidence.
  check [C-xxx | all | branch]    Look for regressions and gaps.
  snapshot [--entry C-xxx]    Capture the worktree and optionally activate a round.
  checkpoint --entry C-xxx --base REF   Record a completed round.
  ask                          Suppress the closeout reminder while awaiting a decision.
  conflicts                    Inspect overlap and reversions.
  why PATH:LINE                Trace a line to a commit and recorded intent.
  lint | stats | backfill
  view [--scope SCOPE] < report.md   Render an offline HTML report.

Branch-local files live under .light-work/. Snapshots use private Git refs
under refs/light-work/. The plugin manages these files and refs automatically.
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

LEDGER_DIR = ".light-work"           # One record per branch under .light-work/.
CP_PREFIX = "refs/light-work/cp/"        # Each checkpoint links an entry to its starting tree.
START_PREFIX = "refs/light-work/start/"  # Initial worktree snapshot.

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
    # A trailing * matches a prefix. Plain words also match plural s. Chinese keywords use substring matching.
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
CONS_RE = re.compile(r"^\s{2,}-\s*([BC]-\d{3,}\.\d+)\s*(\[(?:global|全局)\])?\s*(.*)$")
# Normalize labels at the read boundary. Existing records can mix the two formats.
FIELD_ALIASES = {
    "意图": "Intent", "约束": "Constraints", "假设": "Assumptions",
    "文件": "Files", "定稿": "Files", "提交": "Commit", "级别": "Level",
    "关联": "Related", "验证": "Verification", "过程": "Rounds", "根因": "Root cause",
}
ID_RE = re.compile(r"[BC]-\d{3,}(?:\.\d+)?")
TYPES = {"baseline", "bug", "change", "refactor", "chore"}


# ───────────────────────── git helpers ─────────────────────────

def git(*args, env=None, check=True, cwd=None):
    p = subprocess.run(["git"] + list(args), capture_output=True, text=True, env=env, cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def git_ok(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True).returncode == 0


def repo_root():
    try:
        return git("rev-parse", "--show-toplevel").strip()
    except RuntimeError:
        die("Not in a Git repository.")


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
    """Snapshot tracked and unignored untracked files without changing the real index."""
    idx = git("rev-parse", "--git-path", "index", cwd=root).strip()
    if not os.path.isabs(idx):
        idx = os.path.join(root, idx)
    fd, tmp = tempfile.mkstemp(prefix="lw-index-")
    os.close(fd)
    try:
        if os.path.exists(idx):
            # Preserve the index timestamp so Git detects racy same-size edits.
            shutil.copy2(idx, tmp)
        else:
            os.remove(tmp)
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        git("add", "-A", "--", ".", env=env, cwd=root)  # Exclude .light-work when computing diffs.
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
    """Use built-in defaults without reading user configuration."""
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


def verification_results(parts):
    """Keep unrun checks in the original record and show executed checks in summaries."""
    results = []
    for part in parts:
        if part.startswith("✓"):
            results.append(part)
        elif part.startswith("✗"):
            if re.search(r"未(?:做|跑|测|验|执行|运行|联调|核对)|没(?:做|跑|测|验|执行|运行)|待(?:验|联调)|\b(?:not (?:run|tested|verified|executed)|unverified|pending verification)\b", part, re.I):
                continue
            if re.search(r"→|失败|报错|未通过|\b(?:failed|failure|error)\b", part, re.I):
                results.append(part)
    return results


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
                errors.append("Invalid heading on line %d: %s" % (i, line.strip()))
                cur = None
                continue
            cur = {"id": m.group(1), "date": m.group(2), "type": m.group(3), "status": m.group(4),
                   "title": (m.group(5) or "").strip(), "fields": {}, "constraints": [], "line": i}
            if cur["type"] not in TYPES:
                errors.append("%s has invalid type %s. Expected one of %s." % (cur["id"], cur["type"], "/".join(sorted(TYPES))))
            entries.append(cur)
            cur_field = None
            continue
        if cur is None:
            continue
        cm = CONS_RE.match(line)
        if cm and cur_field == "Constraints":
            cur["constraints"].append({"id": cm.group(1), "global": bool(cm.group(2)),
                                       "text": cm.group(3).strip(), "entry": cur["id"]})
            continue
        fm = FIELD_RE.match(line)
        if fm:
            label = fm.group(1).strip()
            cur_field = FIELD_ALIASES.get(label, label)
            cur["fields"][cur_field] = fm.group(2).strip()
    for e in entries:
        f = e["fields"]
        e["files"] = split_files(f.get("Files", ""))  # Legacy field labels were normalized on read.
        e["shas"] = [s for s in re.findall(r"\b[0-9a-f]{7,40}\b", f.get("Commit", ""))]
        lv = re.search(r"L([0-3])", f.get("Level", ""))
        e["level"] = int(lv.group(1)) if lv else None
        rel = f.get("Related", "")
        sup = []
        for part in re.split(r"[;；]", rel):
            if "supersede" in part or "取代" in part:
                sup += ID_RE.findall(part)
        e["supersedes"] = sup
        e["related"] = [x for x in ID_RE.findall(rel) if x not in sup]
        ver = [s.strip() for s in re.split(r"[｜|]", f.get("Verification", ""))]
        # Preserve the legacy JSON field. Reports and summaries show only executed checks.
        e["unverified"] = [s.strip(" ✗") for s in ver if s.startswith("✗")]
        e["verification_results"] = verification_results(ver)
        e["failed_checks"] = [s[1:].strip() for s in e["verification_results"] if s.startswith("✗")]
        e["assumptions"] = f.get("Assumptions", "")
        e["rounds"] = len(set(re.findall(r"\bR(\d+)", f.get("Rounds", "")))) or (1 if f.get("Rounds") else 0)
        e["verified"] = [s[1:].strip() for s in e["verification_results"] if s.startswith("✓")]
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
    """Whether a path matches the entry's files or directories."""
    for f in files:
        f = f.rstrip("/")
        if path == f or path.startswith(f + "/") or fnmatch.fnmatch(path, f):
            return True
        if "/" not in f and os.path.basename(path) == f:
            return True
    return False


def index_line(e):
    sha = e["shas"][-1][:7] if e["shas"] else "uncommitted"
    if e.get("rounds", 0) > 1:
        sha += " · %d rounds" % e["rounds"]
    lvl = "L%d" % e["level"] if e["level"] is not None else "--"
    return "%s | %s | %s | %s | %s | %s" % (e["id"], e["type"], lvl, e["status"], e["title"] or e["fields"].get("Intent", "")[:30], sha)


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
    """Checkpoints in chronological order, with tree, entry and from."""
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


def entry_ranges(entries, mode, ref, text=str):
    selected = change_entries(entries)
    if mode == "entry":
        if ref is None and selected:
            ref = selected[-1]["id"]
        if ref is None:
            die("No change records to inspect on this branch.")
        selected = [e for e in entries if e["id"] == ref and e["type"] != "baseline"]
        if not selected:
            die("No change record found for %s." % ref)
    ids = {e["id"] for e in selected}
    rounds = [c for c in checkpoint_chain() if c["entry"] in ids and c["from"]]
    recorded = {c["entry"] for c in rounds}
    missing = [e["id"] for e in selected if e["id"] not in recorded]
    if mode == "entry":
        if missing:
            die("%s has no checkpoint. Its diff is unavailable." % ref)
        count = len(rounds)
        label = text("%s (%d completed round)" if count == 1 else "%s (%d completed rounds)") % (ref, count)
    else:
        label = text("All recorded changes (%d entries, %d rounds)") % (len(recorded), len(rounds))
        if missing:
            label += " · " + text("Missing checkpoints: %s") % ", ".join(missing)
    return [(c["from"], c["tree"]) for c in rounds], label, selected


def resolve_range(root, cfg, mode, ref=None, text=str):
    """Return the ranges and label for the worktree diff."""
    br = current_branch()
    cur = worktree_tree(root)
    if mode == "base":
        if not git_ok("rev-parse", "--verify", "-q", ref):
            die("Cannot find ref: %s" % ref)
        return [(git("rev-parse", ref).strip(), cur)], text("%s..worktree") % ref
    if mode == "branch":
        base = detect_base(cfg)
        mb = merge_base(base)
        if mb:
            return [(mb, cur)], text("Full branch (%s merge base %s..worktree)") % (base, mb[:7])
        return [(head_or_empty(), cur)], text("Cannot find %s. Using HEAD..worktree.") % base
    if mode == "ledger":
        sr = start_ref(br)
        if git_ok("rev-parse", "--verify", "-q", sr):
            return [(git("rev-parse", sr + "^{tree}").strip(), cur)], "Change record (starting snapshot..worktree)"
        return resolve_range(root, cfg, "branch", text=text)[0], "No starting snapshot. Using the full branch."
    # Default checkpoint range.
    if git_ok("rev-parse", "--verify", "-q", cp_ref(br)):
        return [(git("rev-parse", cp_ref(br) + "^{tree}").strip(), cur)], "Last checkpoint..worktree"
    if git_ok("rev-parse", "--verify", "-q", start_ref(br)):
        return [(git("rev-parse", start_ref(br) + "^{tree}").strip(), cur)], "Starting snapshot..worktree"
    return [(head_or_empty(), cur)], "HEAD..worktree (no checkpoint yet)"


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
        # Git uses C escapes for special characters in paths.
        value = codecs.escape_decode(value[1:-1].encode("utf-8"))[0].decode("utf-8", errors="replace")
    return value


def parse_diff(root, base, cur):
    """Return {path: {"add","del","lines":[(+/-, text)], "hunks":[...]}}"""
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
            if p.startswith("b/"):  # Deleted files use /dev/null here. Keep the old path.
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
            start, ln_ = (int(m.group(3)), new_len) if new_len else (int(m.group(1)), int(m.group(2) or 1))  # Use old line numbers for deletions.
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


def analyze(root, cfg, mode="checkpoint", ref=None, nhunks=4, current_entry=None, evidence=False, scope_text=str):
    lp = ledger_path(root)
    entries, _ = parse_ledger(lp)
    scoped_entries = entries
    if mode in ("entry", "all_entries"):
        segs, label, scoped_entries = entry_ranges(entries, mode, ref, scope_text)
    else:
        segs, label = resolve_range(root, cfg, mode, ref, scope_text)
        if current_entry:
            scoped_entries = [e for e in change_entries(entries) if e["id"] == current_entry]
            if not scoped_entries:
                die("No active change record found for %s." % current_entry)
            label = scope_text("%s (current round)") % current_entry
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

    # Only removed exports and changed signatures count as interface changes.
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
            iface.append("Removed %s: %s" % (p, g["-"][0].strip()[:80]))
        elif g["+"] and not g["-"]:
            new_exports.append("%s: %s" % (p, name))
        elif set(map(sig, g["-"])) != set(map(sig, g["+"])):
            iface.append("Signature %s: %s → %s" % (p, g["-"][0].strip()[:60], g["+"][0].strip()[:60]))

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

    # Match active constraints and registered paths.
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
        reasons.append("No changes")
    elif not eff_files:
        reasons.append("L0 ← Only tests, docs, lockfiles or assets changed")
    # Report each signal at the highest level it reaches.
    if eff_lines >= L["l3_lines"]:
        bump(3, "%d effective lines ≥ %d" % (eff_lines, L["l3_lines"]))
    elif eff_lines >= L["l1_lines"]:
        bump(2, "%d effective lines ≥ %d" % (eff_lines, L["l1_lines"]))
    elif eff_lines > L["l0_lines"]:
        bump(1, "%d effective lines > %d" % (eff_lines, L["l0_lines"]))
    nf = len(eff_files)
    if nf > L["l3_files"]:
        bump(3, "%d files changed > %d" % (nf, L["l3_files"]))
    elif nf > L["l1_files"]:
        bump(2, "%d files changed > %d" % (nf, L["l1_files"]))
    elif nf > 1:
        bump(1, "%d files changed" % nf)
    if len(modules) >= L["l3_modules"]:
        bump(3, "Across %d modules: %s" % (len(modules), ", ".join(modules)))
    elif len(modules) >= 2:
        bump(2, "Across 2 modules: %s" % ", ".join(modules))
    if iface:
        bump(2, "%d interface changes (removed exports or changed signatures)" % len(iface))
    if config:
        bump(2, "Changed configuration: %s" % ", ".join(config[:4]))
    if hi_strong:
        bump(3, "Sensitive areas: %s" % ", ".join(hi_strong[:6]))
    elif hi_weak:
        bump(2, "Isolated sensitive keywords: %s" % ", ".join(hi_weak))
    if md_hits:
        bump(2, "Relevant keywords: %s" % ", ".join(sorted(md_hits)[:6]))
    if schema:
        bump(3, "Schema, migrations or interfaces: %s" % ", ".join(schema[:4]))
    # Constraints require review at every level without increasing complexity.
    if touched:
        reasons.append("·· Affected constraints: %s (review each one)" % ", ".join(c["id"] for c in touched[:8]))

    # Use git grep to find references to removed or changed definitions.
    reference_tree = segs[-1][1] if segs else None

    def references(name, output_flag):
        out = git("grep", output_flag, "-w", "-I", "-e", name, reference_tree,
                  "--", ".", ":(exclude)%s" % LEDGER_DIR, check=False, cwd=root)
        prefix = reference_tree + ":"
        return [line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)]

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
        refs = [ln for ln in references(n, "-n") if not DEF_RE.search(ln.split(":", 2)[-1])]
        fset = sorted(set(ln.split(":", 1)[0] for ln in refs))
        impact.append({"name": n, "kind": "Removed" if n in removed else "Signature changed", "refs": len(refs),
                       "files": fset[:8], "sample": refs[:3]})

    # Find name matches in unchanged files for functions whose implementation changed.
    # Direct matches are hints and do not affect the level.
    callers = []
    skip = set(removed + resig) | {"for", "while", "switch", "catch", "return", "function", "constructor", "(file scope)"}
    syms = []
    for p in eff_files[:10]:
        for b, c in segs:
            for n in changed_symbols(root, b, c, p):
                if n and n not in skip and len(n) >= 4 and n not in syms:
                    syms.append(n)
    for n in syms[:8]:
        fs = [f for f in references(n, "-l") if f not in diff and not test_re.search(f)]
        if fs:
            callers.append({"name": n, "files": fs[:5], "n": len(fs)})

    flags = []
    for it in impact:
        if it["kind"] == "Removed" and it["refs"]:
            count = it["refs"]
            flags.append("Removed %s still has %d reference%s: %s" % (
                it["name"], count, "" if count == 1 else "s", ", ".join(it["files"][:4])))
    if eff_files and not test_files and eff_lines >= L["notest_lines"]:
        flags.append("%d lines changed without recognized test changes" % eff_lines)
    if eff_del >= 20 and eff_del > 2 * (eff_lines - eff_del):
        flags.append("Mostly deletions (%d lines). Check behavior that callers rely on." % eff_del)
    if unregistered and entries:
        flags.append("%d files are outside the change records" % len(unregistered))

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
        "impact": impact, "callers": callers, "reference_tree": reference_tree,
        "touched_constraints": touched, "global_constraints": globals_, "unregistered": unregistered,
        "entries": scoped_entries, "hunks": top, "ledger": os.path.relpath(lp, root),
        "evidence": diff if evidence else None,
    }


# ───────────────────────── commands ─────────────────────────

LEDGER_HEAD = """# Change record · {branch}

> Base: {base} · Created: {date} · Maintained by ai-light-work for this branch.
> Extend open entries. Append new entries after settlement. To replace a constraint, use `Related: supersedes C-xxx.n`。

"""


def ensure_exclude(root):
    ex = git("rev-parse", "--git-path", "info/exclude", cwd=root).strip()
    if not os.path.isabs(ex):
        ex = os.path.join(root, ex)
    os.makedirs(os.path.dirname(ex), exist_ok=True)
    txt = open(ex, encoding="utf-8").read() if os.path.exists(ex) else ""
    if "/%s/" % LEDGER_DIR not in txt.split():
        with open(ex, "a", encoding="utf-8") as f:
            f.write("\n# ai-light-work (local change records)\n/%s/\n" % LEDGER_DIR)


HOOK_LINE = 'python3 "$HOME/.light-work/lw.py" backfill 2>/dev/null || true  # ai-light-work'


def install_hook(root):
    if git("config", "--get", "core.hooksPath", check=False).strip():
        print("  · Existing core.hooksPath detected. Git's post-commit hook was not installed.")
        return
    hp = git("rev-parse", "--git-path", "hooks/post-commit", cwd=root).strip()
    if not os.path.isabs(hp):
        hp = os.path.join(root, hp)
    txt = open(hp, encoding="utf-8").read() if os.path.exists(hp) else "#!/bin/sh\n"
    if "ai-light-work" in txt:
        return
    os.makedirs(os.path.dirname(hp), exist_ok=True)
    with open(hp, "w", encoding="utf-8") as f:
        f.write(txt.rstrip("\n") + "\n" + HOOK_LINE + "\n")
    os.chmod(hp, 0o755)
    print("  · Installed the post-commit hook to link commits to change records.")


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
    """Create local storage, hook, branch record and initial snapshot if needed."""
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
            note_error("install post-commit hook", e)
    base = detect_base()
    lp = ledger_path(root, br)
    created = False
    if not os.path.exists(lp):
        with open(lp, "w", encoding="utf-8") as f:
            f.write(LEDGER_HEAD.format(branch=br, base=base, date=today()))
        created = True
        say("✓ Created %s (base: %s)" % (os.path.relpath(lp, root), base))
    else:
        say("Already exists: %s" % os.path.relpath(lp, root))
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
    print("Current branch handoff (generated from the change record)")
    print("Branch: %s  Base: %s  Change record: %s" % (br, base, os.path.relpath(lp, root)))
    if not os.path.exists(lp):
        print("No change record yet. Run lw-record for a development change or use init.")
        return
    entries, errors = parse_ledger(lp)
    if errors:
        print("⚠ %d record format issues. Run lint for details." % len(errors))
    act, _ = active_constraints(entries)
    files = a.files or []
    if files:
        rel = [c for c in act if c["global"] or any(path_match(f, c["files"]) for f in files)]
        print("\n## Active constraints for %s (%d/%d)" % (", ".join(files), len(rel), len(act)))
    else:
        rel = act
        print("\n## Active constraints (%d)" % len(act))
    for c in rel[:a.limit]:
        tag = "[global]" if c["global"] else ""
        tent = " (open)" if c["status"] == "open" else ""
        print("- %s%s %s%s  ⟵ %s" % (c["id"], tag, c["text"], tent, ", ".join(c["files"][:3]) or "-"))
    if len(rel) > a.limit:
        print("… %d more. Use --files to narrow the scope." % (len(rel) - a.limit))
    opens = [e for e in entries if e["status"] == "open"]
    if opens:
        print("\n## Open entries (continue the existing entry when intent is the same)")
        for e in opens:
            print("- %s %s · Intent: %s" % (e["id"], e["title"], e["fields"].get("Intent", "")))
            if e["failed_checks"]:
                print("  Recorded validation failures: %s" % "；".join(e["failed_checks"]))
            if e["assumptions"]:
                print("  Assumptions: %s" % e["assumptions"])
    print("\n## Recent entries")
    for e in entries[-8:]:
        print("- " + index_line(e))


def cmd_next_id(a):
    root = repo_root()
    entries, _ = parse_ledger(ledger_path(root))
    pre = "B" if a.baseline else "C"
    nums = [int(e["id"].split("-")[1]) for e in entries if e["id"].startswith(pre)]
    start = 0 if pre == "B" else 1
    print("%s-%03d" % (pre, (max(nums) + 1) if nums else start))


def print_analysis(r, show_hunks=True, check_mode=False):
    print("Scope: %s · Branch: %s" % (r["range"], r["branch"]))
    if not r["eff_files"] and not r["test_files"] and not r["skipped"]:
        print("No changes.")
        return
    print("Level: L%d (%d effective lines / %d files / modules: %s)" % (
        r["level"], r["eff_lines"], len(r["eff_files"]), ", ".join(r["modules"]) or "-"))
    for x in r["reasons"]:
        print("  · " + x)
    for x in r["flags"]:
        print("  ⚠ " + x)
    print("\nFiles: %s" % (", ".join(r["eff_files"]) or "-"))
    if r["test_files"]:
        print("Tests: %s" % ", ".join(r["test_files"]))
    if r["skipped"]:
        print("Skipped: %s" % ", ".join(r["skipped"][:8]))
    if r["iface"]:
        print("\nInterface changes:")
        for x in r["iface"]:
            print("  " + x)
    if r.get("new_exports"):
        print("New exports: %s" % ", ".join(r["new_exports"]))
    if r.get("reference_tree") and (check_mode or r.get("impact") or r.get("callers")):
        print("\nReference snapshot: %s" % r["reference_tree"])
    if r.get("impact"):
        print("\nImpact (git grep, excluding definitions):")
        for it in r["impact"]:
            if it["kind"] != "Removed" and not it["refs"]:
                continue
            print("  - %s %s → %d references %s" % (it["kind"], it["name"], it["refs"], ", ".join(it["files"]) or ""))
    if r.get("callers"):
        print("\nPossible direct callers in unchanged files (name matches):")
        for c in r["callers"]:
            more = " (%d files total)" % c["n"] if c["n"] > len(c["files"]) else ""
            print("  - %s → %s%s" % (c["name"], ", ".join(c["files"]), more))
    tc = r["touched_constraints"]
    if tc or r["global_constraints"]:
        print("\nConstraints to verify (%d affected, %d global):" % (len(tc), len(r["global_constraints"])))
        for c in tc:
            print("  - %s %s  ⟵ %s%s" % (c["id"], c["text"], ", ".join(c["files"][:3]),
                                        " (open)" if c["status"] == "open" else ""))
        for c in r["global_constraints"]:
            print("  - %s[global] %s" % (c["id"], c["text"]))
    if check_mode:
        ents = [e for e in r["entries"] if e["status"] != "dropped"]
        opens = [e for e in ents if e["status"] == "open"]
        failures = [(e["id"], result) for e in ents for result in e["failed_checks"]]
        nosha = [e["id"] for e in ents if e["type"] != "baseline" and not e["shas"]]
        asm = [(e["id"], e["assumptions"]) for e in ents if e["assumptions"] and e["assumptions"] not in ("None", "无", "-")]
        if opens:
            print("\nOpen entries: %s" % ", ".join("%s %s" % (e["id"], e["title"]) for e in opens))
        if failures:
            print("Recorded validation failures:")
            for i, result in failures:
                print("  - %s: %s" % (i, result))
        if asm:
            print("Unconfirmed assumptions:")
            for i, s in asm:
                print("  - %s: %s" % (i, s))
        if nosha:
            print("Entries without commits: %s" % ", ".join(nosha))
        if r["unregistered"]:
            print("Files outside the change records: %s" % ", ".join(r["unregistered"]))
    if show_hunks and r["hunks"]:
        print("\nSelected Git diff excerpts (largest first):")
        for h in r["hunks"]:
            print("\n▸ %s  %s (%d lines changed)" % (h["loc"], h["ctx"], h["changed"]))
            for b in h["body"]:
                print("    " + b)
            if h["truncated"]:
                print("    …")
    print("\nChange record: %s" % r["ledger"])


def strip_for_json(r):
    out = dict(r)
    out.pop("evidence", None)
    out["entries"] = [{k: e[k] for k in ("id", "type", "status", "title", "level", "shas", "unverified", "failed_checks", "files")}
                      for e in r["entries"]]
    return out


def parse_scope(value):
    if value in ("all", "a"):
        return "all_entries", None
    if value in ("branch", "b"):
        return "branch", None
    if re.fullmatch(r"C-\d{3,}", value):
        return "entry", value
    raise argparse.ArgumentTypeError("Scope must be C-xxx, all, or branch.")


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
            print("\nChange record format issues:")
            for e in errors:
                print("  - " + e)


def state_path(root):
    # A worktree has one active round. Branch history lives in the separate ledger.
    path = git("rev-parse", "--git-path", "light-work/round.json", cwd=root).strip()
    return os.path.join(root, path)


def state_load(root):
    try:
        with open(state_path(root), encoding="utf-8") as file:
            state = json.load(file)
    except (FileNotFoundError, ValueError):
        return {}
    return state if state.get("branch") == current_branch() else {}


def state_save(root, state):
    path = state_path(root)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def state_clear(root):
    try:
        os.unlink(state_path(root))
    except FileNotFoundError:
        pass


def cmd_checkpoint(a):
    root = repo_root()
    cfg = load_cfg(root)
    br = current_branch()
    segs, _ = resolve_range(root, cfg, "base" if a.base else "checkpoint", a.base)
    frm, tree = segs[0]
    frm_tree = git("rev-parse", frm + "^{tree}").strip()
    state = state_load(root)
    closes_round = state.get("entry") == a.entry and state.get("base") == frm_tree
    if frm_tree == tree and not a.force:
        print("No new changes. Checkpoint unchanged.")
        if closes_round:
            state_clear(root)
        return
    ref = cp_ref(br)
    parent = git("rev-parse", ref).strip() if git_ok("rev-parse", "--verify", "-q", ref) else None
    base_parent = None
    if a.base and (not parent or frm_tree != git("rev-parse", parent + "^{tree}").strip()):
        # Keep the starting tree for later entry-scoped diffs.
        base_parent = make_commit(frm_tree, "light-work round start")
    msg = "ledger checkpoint entry=%s from=%s at=%s" % (a.entry or "-", frm_tree,
                                                         _dt.datetime.now().isoformat(timespec="seconds"))
    git("update-ref", ref, make_commit(tree, msg, parent, base_parent))
    if closes_round:
        state_clear(root)
    print("✓ Checkpoint recorded (%s%s). Subsequent score starts here." % (
        tree[:7], ", entry " + a.entry if a.entry else ""))


def cmd_snapshot(a):
    root = repo_root()
    if not a.entry:
        if a.base:
            die("--base requires --entry.")
        print(worktree_tree(root))
        return
    entries, _ = parse_ledger(ledger_path(root))
    if not any(e["id"] == a.entry and e["status"] == "open" for e in change_entries(entries)):
        die("No open change record found for %s." % a.entry)
    base = git("rev-parse", "--verify", "--end-of-options", a.base + "^{tree}").strip() if a.base else worktree_tree(root)
    state_save(root, {"branch": current_branch(), "entry": a.entry, "base": base, "ctx_shown": {}})
    print(base)


def cmd_lint(a):
    root = repo_root()
    lp = ledger_path(root)
    if not os.path.exists(lp):
        die("No change record: %s" % os.path.relpath(lp, root))
    entries, errors = parse_ledger(lp)
    ids = [e["id"] for e in entries]
    all_ids = set(ids) | {c["id"] for e in entries for c in e["constraints"]}
    seen = set()
    for e in entries:
        if e["id"] in seen:
            errors.append("Duplicate entry ID: %s" % e["id"])
        seen.add(e["id"])
        f = e["fields"]
        for need in ("Intent", "Verification"):
            if need not in f:
                errors.append("%s is missing %s." % (e["id"], need))
        if e["type"] != "baseline" and "Level" not in f:
            errors.append("%s is missing Level (use Pending when recording the intent)." % e["id"])
        if e["status"] == "settled" and e["type"] != "baseline" and e["level"] is None:
            errors.append("%s is settled but its level is pending." % e["id"])
        if e["status"] == "settled" and e["type"] != "baseline" and not e["files"]:
            errors.append("%s is settled but has no Files field." % e["id"])
        if e["level"] is not None and e["level"] >= 1 and not e["constraints"] and e["type"] != "chore":
            errors.append("%s is L%d and needs at least one testable constraint." % (e["id"], e["level"]))
        for c in e["constraints"]:
            if not c["id"].startswith(e["id"] + "."):
                errors.append("Constraint %s does not belong to %s." % (c["id"], e["id"]))
            if len(c["text"]) < 6 or re.search(r"(处理好|注意|优化|合理|正确处理)", c["text"]):
                errors.append("Constraint %s is too vague to verify: %s" % (c["id"], c["text"]))
        if e["type"] == "bug" and e["status"] == "settled" and f.get("Root cause", "").strip().lower() in ("", "pending", "待查"):
            errors.append("%s is a settled bug and needs a root cause." % e["id"])
        for v in e["verified"]:
            if re.search(r"(应该|理论上|估计|看起来|大概|should|probably|seems)", v, re.I):
                errors.append("%s verification is not evidence: %s. Record only executed checks and results." % (e["id"], v))
        for s in e["supersedes"] + e["related"]:
            if s not in all_ids:
                errors.append("%s links to missing entry or constraint %s." % (e["id"], s))
    if errors:
        print("✗ %d issues:" % len(errors))
        for x in errors:
            print("  - " + x)
        sys.exit(1)
    print("✓ Change record valid (%d entries)" % len(entries))


def cmd_backfill(a):
    root = repo_root()
    lp = ledger_path(root)
    if not os.path.exists(lp):
        return
    sha = git("rev-parse", "--short", a.commit).strip()
    files = set(git("diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", a.commit).split("\0")) - {""}
    entries, _ = parse_ledger(lp)
    targets = [e for e in entries if e["type"] != "baseline" and e["status"] != "dropped"
               and (e["status"] == "open" or not e["shas"])
               and any(path_match(f, e["files"]) for f in files) and sha not in e["shas"]]
    if not targets:
        return
    expected = {e["id"]: {} for e in targets}
    for round_ in checkpoint_chain():
        if round_["entry"] not in expected:
            continue
        changed = git("diff", "--name-only", "--no-renames", "-z", round_["from"], round_["tree"]).split("\0")
        for path in filter(None, changed):
            expected[round_["entry"]][path] = round_["tree"]

    # Check the committed content against the checkpoint before linking an entry.
    differences = {}
    tid = set()
    for entry, paths in expected.items():
        if files.isdisjoint(paths):
            continue
        for path, tree in paths.items():
            if tree not in differences:
                differences[tree] = set(git("diff", "--name-only", "--no-renames", "-z", tree, a.commit).split("\0"))
            if path in differences[tree]:
                break
        else:
            tid.add(entry)

    if not tid:
        return
    lines = open(lp, encoding="utf-8").read().splitlines()
    settle = {e["id"] for e in targets if e["id"] in tid and e["status"] == "open" and e["level"] is not None}
    entries_with_commit_field = {e["id"]: "Commit" in e["fields"] for e in targets}
    out, cur, done = [], None, set()
    for ln in lines:
        m = HEADER_RE.match(ln.strip()) if ln.startswith("### ") else None
        if m:
            cur = m.group(1)
            # Settle reviewed entries whose checkpoint matches the commit.
            if cur in settle:
                ln = ln.replace(" · open", " · settled", 1)
        if cur in tid and cur not in done and FIELD_RE.match(ln):
            label = FIELD_RE.match(ln).group(1).strip()
            if FIELD_ALIASES.get(label, label) == "Commit":
                val = FIELD_RE.match(ln).group(2).strip()
                val = "" if val.startswith(("(", "（", "待")) else val
                ln = "- %s: %s" % (label, ", ".join(x for x in (val, sha) if x))
                done.add(cur)
        out.append(ln)
        # Insert a missing commit field after the files field, using the same language.
        field = FIELD_RE.match(ln)
        if cur in tid and cur not in done and field:
            label = field.group(1).strip()
            if FIELD_ALIASES.get(label, label) == "Files" and not entries_with_commit_field.get(cur):
                commit_label = "提交" if label in FIELD_ALIASES else "Commit"
                out.append("- %s: %s" % (commit_label, sha))
                done.add(cur)
    open(lp, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("ai-light-work: Linked %s to %s" % (sha, ", ".join(sorted(done))))


def cmd_stats(a):
    root = repo_root()
    entries, _ = parse_ledger(ledger_path(root))
    ch = [e for e in entries if e["type"] != "baseline"]
    if not ch:
        print("No change entries yet.")
        return
    cnt = {i: 0 for i in range(4)}
    for e in ch:
        if e["level"] is not None:
            cnt[e["level"]] += 1
    n = sum(cnt.values()) or 1
    print("%d entries · " % len(ch) + " · ".join("L%d %d" % (i, cnt[i]) for i in range(4)))
    by_type = {}
    for e in ch:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print("Types: " + " · ".join("%s %d" % kv for kv in by_type.items()))
    print("%d open · %d recorded validation failures" % (sum(e["status"] == "open" for e in ch),
                                               sum(len(e["failed_checks"]) for e in ch)))
    if cnt[3] / n > 0.3:
        print("⚠ %.0f%% of entries are L3. Consider splitting the work into smaller changes." % (100.0 * cnt[3] / n))


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
    """Find enclosing symbols using each edit and the closest preceding definition."""
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
        syms.add(name or ctx_sym or "(file scope)")
        if ln[0] == "-":
            o_ln += 1
        else:
            n_ln += 1
    return syms


def cmd_conflicts(a):
    """Find overlapping, reverted and frequently revised changes."""
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
            e = "(unassigned)"
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
    # Hotspots are file/symbol pairs shared by at least two entries.
    hot = {}
    for e in order:
        for k in per[e]["keys"]:
            hot.setdefault(k, [])
            if e not in hot[k]:
                hot[k].append(e)
    hotspots = sorted([(k, v) for k, v in hot.items() if len(v) >= 2], key=lambda x: -len(x[1]))
    # Find earlier additions that later entries removed.
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
    # Pending changes
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
    print("Change record: %s · %d entries with diffs (%s)" % (os.path.relpath(lp, root), len(order), ", ".join(order) or "-"))
    if not chain:
        print("No checkpoints yet. Record a closeout before checking conflicts between entries.")
    if pending:
        print("⚠ There are unclosed changes since the last checkpoint.")
    if unowned:
        print("⚠ %d rounds have no entry (checkpoint had no --entry)." % unowned)
    if reverts:
        print("\nReversions and rewrites (later entries removed earlier additions):")
        for r in reverts:
            print("  - %s removed or rewrote %d lines added by %s in %s. Example: %s" % (
                r["later"], r["n"], r["earlier"], r["file"], " | ".join(x[:60] for x in r["sample"])))
    if hotspots:
        print("\nHotspots (shared by multiple entries):")
        for h in res["hotspots"][:15]:
            print("  - %s · %s ← %s" % (h["file"], h["symbol"], " → ".join(h["entries"])))
    if many_rounds:
        print("\nFrequently revised entries (3+ rounds): %s" % ", ".join("%s×%d" % x for x in many_rounds))
    if sup:
        print("\nSuperseded constraints: %s" % "；".join("%s supersedes %s" % (i, ",".join(v)) for i, v in sup))
    if cons_on_hot:
        print("\nActive constraints to verify at hotspots:")
        for c in cons_on_hot:
            print("  - %s%s %s" % (c["id"], "[global]" if c["global"] else "", c["text"]))
    if not (reverts or hotspots or many_rounds):
        print("\nNo overlap or reversions found.")


# ───────────────────────── plugin hooks─────────────────────────

WORKFLOW = """[ai-light-work] When the user asks you to edit code, configuration, tests, or development docs in a Git worktree, use lw-record before editing, even for a single-file change. Then use lw-edit and lw-review. Leave read-only requests and unrelated work outside this workflow."""


ERR_FILE = os.path.expanduser("~/.light-work/hook-error.txt")
PENDING_ERR = []   # Pending notice from a previous hook failure.
EMITTED = []


def note_error(where, e):
    """Record hook failures for a one-time notice without interrupting the session."""
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
    return ("[ai-light-work] A previous hook failed (%s). The change record may be incomplete. "
            "Mention this briefly at the start of your reply and point to fahcode/ai-light-work for feedback." % msg) if msg else None


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
    """Copy the script to the stable path shared by skills and Git hooks."""
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
            os.replace(tmp, dst)  # Replace atomically so other hosts never read a partial script.
    except Exception as e:
        note_error("copy ~/.light-work/lw.py", e)


def rel_to_root(root, path):
    if not path:
        return None
    ap = os.path.realpath(path)  # Resolve symlinks before making the path relative to the worktree root.
    try:
        rp = os.path.relpath(ap, os.path.realpath(root))
    except ValueError:
        return None
    return None if rp.startswith("..") else rp.replace(os.sep, "/")


# Codex apply_patch embeds file paths in the patch text.
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to):\s*(.+?)\s*$", re.M)


def edited_paths(root, data):
    """Return worktree-relative paths edited by the tool, excluding the record directory.
    Claude Code and CodeBuddy supply file paths. Codex supplies a patch."""
    ti = data.get("tool_input")
    cands = []
    if isinstance(ti, dict):
        cands = [ti[k] for k in ("file_path", "notebook_path", "path") if isinstance(ti.get(k), str) and ti[k]]
    if not cands:  # Hosts use different envelope keys for the patch text.
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
    emit("SessionStart", WORKFLOW)


def hook_prompt(root, data):
    # A new user task must opt in again through lw-record, even with open history.
    state_clear(root)


def hook_pre_edit(root, data):
    st = state_load(root)
    if not st:
        return
    files = edited_paths(root, data)
    if not files:
        return
    entries, _ = parse_ledger(ledger_path(root))
    act, _ = active_constraints(entries)
    msgs = []
    cached = st.get("ctx_shown")
    shown = {key: texts for key, texts in cached.items() if isinstance(texts, dict)} if isinstance(cached, dict) else {}
    shown.setdefault("*", {})
    glb = [c for c in act if c["global"]]
    for f in files:  # A Codex patch can touch multiple files.
        shown.setdefault(f, {})
        rel = [c for c in act if not c["global"] and path_match(f, c["files"])]
        new = [c for c in glb + rel if shown["*" if c["global"] else f].get(c["id"]) != c["text"]]
        if new:
            msgs.append("Constraints to preserve when editing %s:" % f)
            for c in new[:12]:
                msgs.append("- %s%s %s" % (c["id"], "[global]" if c["global"] else "", c["text"]))
                shown["*" if c["global"] else f][c["id"]] = c["text"]
    if msgs:
        msgs.append("If the edit clearly conflicts with an active constraint the user did not address, ask using lw-edit's format before editing.")
    st["ctx_shown"] = shown
    if msgs:
        state_save(root, st)
        emit("PreToolUse", "[ai-light-work] " + "\n".join(msgs))


def hook_stop(root, data):
    if data.get("stop_hook_active"):
        return
    st = state_load(root)
    if not st or st.get("awaiting"):
        return
    # Compare only a recorded round, including edits made through shell commands.
    cur = worktree_tree(root)
    if cur in (st["base"], st.get("nag_tree")):
        return
    st["nag_tree"] = cur
    state_save(root, st)
    emit("Stop", extra={"systemMessage": "[ai-light-work] %s has changes with no closeout yet. Run lw-review to finish." % st["entry"]})


def cmd_why(a):
    """Trace a line from Git blame to its commit and change record."""
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
        for k in ("Intent", "Root cause"):
            if f.get(k) and f[k] not in ("Pending", "待查", "-", "None", "无"):
                print("    %s: %s" % (k, f[k]))
        for c in e["constraints"]:
            print("    %s %s%s" % (c["id"], c["text"], " (superseded)" if c["id"] in dead else ""))
        if later.get(e["id"]):
            print("    Later superseded or reworked by: %s" % ", ".join(later[e["id"]]))

    for spec in a.targets:
        m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", spec)
        if not m:
            die("Expected PATH:LINE or PATH:START-END, such as src/a.ts:42.")
        path = rel_to_root(root, m.group(1)) or m.group(1)
        lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
        raw = git("blame", "--porcelain", "-L", "%d,%d" % (lo, hi), "--", path, check=False, cwd=root)
        shas = []
        for ln in raw.splitlines():
            mm = re.match(r"^([0-9a-f]{40}) \d+ \d+", ln)
            if mm and mm.group(1) not in shas:
                shas.append(mm.group(1))
        if not shas:
            print("%s: git blame returned no results. Check the path and line numbers." % spec)
            continue
        by_file = [e for e in entries if e["type"] != "baseline" and path_match(path, e["files"])]
        for sha in shas:
            if set(sha) == {"0"}:
                print("%s ← Uncommitted change" % spec)
                hits = [e for e in by_file if e["status"] == "open"] or by_file[-1:]
                for e in hits:
                    show(e, path)
                if not hits:
                    print("  No change record lists this file.")
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
                print("  This line was already on the base branch.")
            elif start_ct and int(git("log", "-1", "--format=%ct", sha, check=False).strip() or 0) <= start_ct:
                print("  This commit predates the starting snapshot and has no per-entry record.")
            else:
                print("  This commit is not linked to a change record.")
                for e in by_file[-3:]:
                    print("  Entry for this file: %s %s" % (e["id"], e["title"]))


def cmd_ask(a):
    """Suppress this round's reminder while waiting for a conflict decision."""
    root = repo_root()
    st = state_load(root)
    if st:
        st["awaiting"] = True
        state_save(root, st)
        print("✓ Marked as awaiting a user decision")


def cmd_hook(a):
    if a.event == "post-edit":  # Older installed hook configurations may still call this event.
        return
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
         "stop": hook_stop}[a.event](root, data)
    except SystemExit:
        pass
    except Exception as e:  # Report the failure on the next prompt without interrupting this session.
        if os.environ.get("LW_DEBUG"):
            raise
        sys.stderr.write("ai-light-work hook %s: %s\n" % (a.event, e))
        note_error("Hook " + a.event, e)
    if PENDING_ERR and not EMITTED:
        emit(ev)
    sys.exit(0)


# Only renderer-owned labels are translated. The caller supplies the authored report.
REPORT_ZH = {
    "%s (current round)": "%s 的本轮改动",
    "%s (%d completed round)": "%s 的 %d 轮已收尾改动",
    "%s (%d completed rounds)": "%s 的 %d 轮已收尾改动",
    "All recorded changes (%d entries, %d rounds)": "全部已记录改动（%d 条，%d 轮）",
    "Missing checkpoints: %s": "缺少收尾点：%s",
    "%s..worktree": "%s..工作区",
    "Full branch (%s merge base %s..worktree)": "全分支（%s 合并基点 %s..工作区）",
    "Cannot find %s. Using HEAD..worktree.": "找不到 %s，使用 HEAD..工作区。",
    "Change review": "改动审阅",
    "Report navigation": "报告导航",
    "Original change record": "原始改动记录",
    "Recorded intent and verification": "记录中的意图与验证",
    "Intent": "意图",
    "Verification": "验证",
    "Review summary": "审阅结论",
    "Code evidence": "代码证据",
    "Back to review": "返回审阅",
    "No file changes in this scope.": "所选范围没有文件改动。",
    "Browse files": "按文件浏览",
    "Filter files": "筛选文件",
    "No matching files": "没有匹配的文件",
    "%d file": "%d 个文件",
    "%d files": "%d 个文件",
    "Binary file": "二进制文件",
    "Patch omitted. File totals are available above.": "已省略补丁，上方可查看文件统计。",
    "Omitted %d lines": "省略 %d 行",
    "Omitted %d trailing lines": "省略末尾 %d 行",
    "Line truncated": "此行已截断",
    "Old and new line numbers and patch": "新旧行号和补丁",
    "No text patch.": "无文本补丁。",
    "%d more hunks. Use Git for the full diff.": "另有 %d 个改动片段，可用 Git 查看完整 diff。",
    "Points to": "指向",
    "Flowchart": "流程图",
    "Sequence diagram": "时序图",
    "Dependency diagram": "依赖图",
    "Mermaid source": "Mermaid 源码",
}


def language_tag(value):
    if not re.fullmatch(r"(?:[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*|[ix](?:-[A-Za-z0-9]{1,8})+)", value, re.I):
        raise argparse.ArgumentTypeError("Use a language tag such as en, zh-Hans or fr-CA.")
    return value


def report_locale(lang):
    parts = lang.lower().split("-")
    if parts[0] == "zh":
        if len(parts) == 1 or parts[1] not in ("hant", "tw", "hk", "mo"):
            return "zh-Hans", lambda message: REPORT_ZH.get(message, message)
    return "en", str


VIEW_HTML = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title{title_attr}>{title}</title>
<style>
:root {{
  color-scheme: light dark;
  --background: #f3f3ef; --panel: #fff; --surface: #f7f8f5; --text: #172321;
  --muted: #505e58; --border: #d8dfd9; --link: #136354; --accent: #e5f3ed;
  --add: #e7f6ec; --del: #fbeae8;
  --flow-ink: #145e50; --flow-wash: #eef7f3; --flow-rule: #adcfc3;
  --sequence-ink: #4e4786; --sequence-wash: #f4f1fb; --sequence-rule: #cdc4e6;
  --architecture-ink: #2b537a; --architecture-wash: #eff5fb; --architecture-rule: #b7cde1;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --background: #131209; --panel: #181818; --surface: #1f1f1f; --text: #f0f3ee;
    --muted: #b3bcb5; --border: #414943; --link: #a1dfc9; --accent: #203b31;
    --add: #20362a; --del: #432a29;
    --flow-ink: #a1dfc9; --flow-wash: #1d3028; --flow-rule: #466b5d;
    --sequence-ink: #ddd4fc; --sequence-wash: #2f2a3d; --sequence-rule: #655981;
    --architecture-ink: #bfddfa; --architecture-wash: #233244; --architecture-rule: #4b6380;
  }}
}}
* {{ box-sizing: border-box }}
[hidden] {{ display: none !important }}
body {{ margin: 0; background: var(--background); color: var(--text); font: 1rem/1.5 Geist, system-ui, sans-serif }}
.layout {{ max-width: 86rem; margin-inline: auto; padding: 2.5rem 2rem; display: grid;
  grid-template-columns: 14rem minmax(0, 1fr); gap: 2rem }}
main {{ min-width: 0 }}
nav {{ position: sticky; top: 2rem; max-height: calc(100vh - 4rem); overflow: auto;
  align-self: start; padding: 1rem; background: var(--panel); border: 1px solid var(--border);
  border-radius: 1rem; font-size: .875rem; line-height: 1.25rem }}
nav a {{ display: block; padding: .5rem; color: var(--text); overflow-wrap: anywhere;
  border-radius: .5rem; text-decoration: none }}
nav a[aria-current], nav a:hover {{ background: var(--accent); color: var(--link) }}
nav .nav-section {{ font-weight: 600 }}
nav .nav-file strong, nav .nav-file small {{ display: block }}
nav .nav-file small {{ font-size: .75rem; line-height: 1rem }}
.file-nav {{ border-top: 1px solid var(--border); margin-top: .75rem; padding-top: .75rem }}
.file-nav summary {{ padding: .5rem }}
.file-nav input {{ color: var(--text); background: var(--surface); width: 100%; padding: .5rem;
  margin: .25rem 0; border: 1px solid var(--border); border-radius: .5rem; font: inherit }}
.file-nav p {{ padding: 0 .5rem; margin: .5rem 0 }}
a {{ color: var(--link); text-underline-offset: .2em }}
a:focus-visible, summary:focus-visible, input:focus-visible {{ outline: 2px solid var(--link); outline-offset: 2px }}
.overview, .panel {{ background: var(--panel); border: 1px solid var(--border);
  border-radius: 1rem; padding: clamp(1rem, 3vw, 2rem); margin-bottom: 1rem }}
.overview {{ background: var(--accent) }}
h1, h2, h3 {{ text-wrap: balance }}
h1 {{ font-size: clamp(1.5rem, 4vw, 2.25rem); line-height: 1.15; margin: .25rem 0 .75rem }}
h2 {{ font-size: 1.25rem; line-height: 1.75rem; margin: 0 0 .75rem }}
h3 {{ font-size: 1.125rem; line-height: 1.75rem; margin: 0 }}
p, li {{ text-wrap: pretty }}
p, li, th, td, dd, .meta, h1, h2, h3 {{ overflow-wrap: anywhere }}
.meta, small {{ color: var(--muted) }}
.meta {{ font-size: .875rem; line-height: 1.25rem }}
.facts {{ display: flex; gap: .5rem 1rem; flex-wrap: wrap; margin: 0; padding: 0;
  color: var(--muted); font-size: .875rem; line-height: 1.25rem; font-variant-numeric: tabular-nums }}
.facts li {{ list-style: none }}
.record-summary dl {{ margin: 1rem 0 0; display: grid;
  grid-template-columns: max-content minmax(0, 1fr); gap: .25rem 1rem }}
dt {{ font-weight: 600 }} dd {{ margin: 0 }}
.report > * {{ margin-block: 0 .75rem }}
.report > h2:not(:first-child) {{ margin-block-start: 1.5rem; padding-block-start: 1rem; border-top: 1px solid var(--border) }}
.report > :first-child + p {{ font-size: 1.125rem; line-height: 1.75rem }}
.report > :last-child {{ margin-bottom: 0 }}
blockquote {{ padding: .75rem; background: var(--accent); border-radius: .5rem }}
code, pre, .numbers {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace }}
pre {{ margin: 0; overflow: auto; white-space: pre; tab-size: 4 }}
.report pre, #ledger-raw pre {{ background: var(--surface); padding: .75rem }}
.table {{ overflow: auto; border: 1px solid var(--border); border-radius: .5rem }}
table {{ width: 100%; border-collapse: collapse }}
th, td {{ text-align: left; vertical-align: top; padding: .5rem .75rem; border-bottom: 1px solid var(--border) }}
th {{ background: var(--surface) }}
tr:last-child td {{ border-bottom: 0 }}
summary {{ cursor: pointer; font-weight: 600; overflow-wrap: anywhere }}
.evidence-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: .75rem }}
.file {{ margin: .75rem 0; border: 1px solid var(--border); border-radius: .5rem }}
.file summary {{ padding: .75rem }}
.file[open] summary {{ background: var(--surface); border-bottom: 1px solid var(--border) }}
.file > p {{ padding-inline: .75rem }}
.file .meta {{ font-weight: 400; margin-inline-start: .75rem }}
.hunk {{ padding-block: .5rem }}
.hunk + .hunk {{ border-top: 1px solid var(--border) }}
.hunk header {{ font: .875rem/1.25rem ui-monospace, monospace; padding: .5rem .75rem;
  background: var(--surface); overflow-wrap: anywhere }}
.hunk pre {{ font-size: .875rem; line-height: 1.25rem }}
.line {{ display: block; min-width: max-content; padding-inline-end: 1rem }}
.line.add {{ background: var(--add) }} .line.del {{ background: var(--del) }}
.omitted {{ padding: .5rem .75rem; color: var(--muted); background: var(--surface) }}
.numbers {{ display: inline-block; min-width: 12ch; padding-inline: .5rem; color: var(--muted); user-select: none }}
:target {{ outline: 2px solid var(--link); outline-offset: 2px; scroll-margin-block: 1rem }}
.flow {{ display: flex; gap: .75rem; flex-wrap: wrap; padding: 0; list-style: none; counter-reset: step }}
.flow li {{ padding: .5rem .75rem; border-radius: .5rem; background: var(--accent); counter-increment: step }}
.flow li::before {{ content: counter(step) ". "; color: var(--link); font-weight: 600 }}
.diagram {{ --diagram-ink: var(--flow-ink); --diagram-wash: var(--flow-wash); --diagram-rule: var(--flow-rule);
  margin: 0 0 1rem; padding: 1rem; border: 1px solid var(--diagram-rule);
  border-radius: .75rem; background: var(--diagram-wash) }}
.diagram.sequence {{ --diagram-ink: var(--sequence-ink); --diagram-wash: var(--sequence-wash);
  --diagram-rule: var(--sequence-rule) }}
.diagram.architecture {{ --diagram-ink: var(--architecture-ink); --diagram-wash: var(--architecture-wash);
  --diagram-rule: var(--architecture-rule) }}
.diagram figcaption {{ margin-bottom: .75rem; color: var(--diagram-ink); font-weight: 600 }}
.diagram ol, .diagram ul {{ display: grid; gap: .5rem; margin: 0; padding: 0; list-style: none }}
.diagram .edge {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .5rem;
  align-items: center; overflow-wrap: anywhere }}
.diagram .node {{ background: var(--panel); border: 1px solid var(--diagram-rule);
  border-radius: .5rem; padding: .5rem .75rem; font-weight: 600 }}
.diagram .connector {{ color: var(--diagram-ink); text-align: center; font-size: .875rem; line-height: 1.25rem }}
.diagram .arrow {{ display: flex; align-items: center; gap: .25rem; font-size: 1.25rem; line-height: 1.25rem }}
.diagram .arrow::before {{ content: ""; flex: 1; border-top: 1px solid currentColor }}
.sequence ol {{ counter-reset: message }}
.sequence .edge {{ counter-increment: message }}
.sequence .connector::before {{ content: counter(message) ". "; font-weight: 600 }}
.flowchart .group {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: .5rem; align-items: center }}
.diagram .group .node {{ min-width: 0; overflow-wrap: anywhere }}
.diagram .group .edge {{ grid-template-columns: repeat(2, minmax(0, 1fr)) }}
.architecture .group {{ border: 1px solid var(--diagram-rule); background: var(--panel);
  border-radius: .5rem; padding: .75rem }}
.architecture h3 {{ margin-bottom: .5rem; color: var(--diagram-ink) }}
@media (max-width: 48rem) {{
  .layout {{ display: block; padding: .75rem }}
  nav {{ position: static; max-height: 14rem; margin-bottom: .75rem }}
  .nav-sections {{ display: flex; flex-wrap: wrap; gap: .25rem }}
  .flowchart .group {{ grid-template-columns: minmax(0, 1fr); gap: .25rem }}
  .flowchart .group + .group {{ margin-top: .5rem }}
  .table th, .table td {{ min-width: 7rem }}
}}
</style></head>
<body><div class="layout">
<nav aria-label="{navigation}"{ui_attr}>{nav}</nav>
<main>
<header class="overview"><div class="meta"{ui_attr}>{scope}</div><h1{title_attr}>{title}</h1>{facts}</header>
{report}
{evidence}
{overview}
<details class="panel" id="ledger-raw"><summary{ui_attr}>{record_label}</summary><pre translate="no">{ledger}</pre></details>
</main></div>
<script>
function reveal() {{
  const hash = location.hash || (document.getElementById('report') ? '#report' : '#evidence');
  const target = document.getElementById(hash.slice(1));
  const file = target?.closest('.file');
  const section = target?.id === 'report' ? target.querySelector('h2[id]') : target;
  const active = '#' + (file?.id || section?.id || target?.id);
  document.querySelectorAll('nav a').forEach(link => {{
    if (link.hash === active) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }});
  if (!location.hash) return;
  if (!target) return;
  const details = target.closest('details');
  if (details) details.open = true;
  target.setAttribute('tabindex', '-1');
  target.focus({{preventScroll: true}});
  target.scrollIntoView();
}}
const filter = document.getElementById('file-filter');
if (filter) {{
  const links = Array.from(document.querySelectorAll('.nav-file'));
  const empty = document.getElementById('file-filter-empty');
  filter.hidden = false;
  if (matchMedia('(max-width: 48rem)').matches) filter.closest('details').open = false;
  function filterFiles() {{
    const query = filter.value.trim().toLowerCase();
    let matches = 0;
    links.forEach(link => {{
      link.hidden = !link.getAttribute('aria-label').toLowerCase().includes(query);
      if (!link.hidden) matches++;
    }});
    empty.hidden = matches !== 0;
  }}
  filter.addEventListener('input', filterFiles);
  filterFiles();
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
    # Keep cited lines and nearby context first, then the beginning of the hunk.
    for indexes in ([i + distance for i in matches for distance in (-1, 1, -2, 2)], range(limit)):
        for i in indexes:
            if len(selected) == limit:
                break
            if 0 <= i < len(rows):
                selected.add(i)
    return [(i + 1, rows[i]) for i in sorted(selected)]


def render_evidence(files, skipped, references, text):
    nav, panels, locations = [], [], {}
    for i, (path, diff) in enumerate(sorted(files.items()), 1):
        file_id = "file-%d" % i
        label = escape(path)
        folder, _, name = path.rpartition("/")
        directory = f"<small>{escape(folder)}</small>" if folder else ""
        nav.append(f'<a class="nav-file" href="#{file_id}" aria-label="{label}" translate="no">'
                   f'<strong>{escape(name)}</strong>{directory}</a>')
        locations[path] = (file_id, {})
        counts = text("Binary file") if diff["binary"] else "+%d / −%d" % (diff["add"], diff["del"])
        if path in skipped:
            panels.append(f'<details class="file" id="{file_id}"><summary><code translate="no">{label}</code>'
                          f'<span class="meta">{counts}</span></summary>'
                          f'<p>{text("Patch omitted. File totals are available above.")}</p></details>')
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
            for n, (kind, old, new, line) in patch_excerpt(hunk, cited):
                if n > preceding + 1:
                    rows.append('<span class="line omitted">%s</span>' % (text("Omitted %d lines") % (n - preceding - 1)))
                preceding = n
                row_id = "%s-line-%d" % (hunk_id, n)
                if old is not None:
                    locations[path][1].setdefault(old, []).append((False, j, row_id))
                if new is not None:
                    locations[path][1].setdefault(new, []).append((True, j, row_id))
                numbers = "%s %s" % (str(old or "").rjust(5), str(new or "").rjust(5))
                tail = f'<span class="meta"> … ({text("Line truncated")})</span>' if len(line) > 600 else ""
                rows.append(f'<span class="line {kind}" id="{row_id}"><span class="numbers">{numbers}</span>{escape(line[:600])}{tail}</span>')
            if preceding < len(hunk["body"]):
                rows.append('<span class="line omitted">%s</span>' %
                            (text("Omitted %d trailing lines") % (len(hunk["body"]) - preceding)))
            body = "".join(rows)
            header = escape(hunk["revision"] + "  " + hunk["header"])
            patch_label = text("Old and new line numbers and patch")
            hunks.append(f'<section class="hunk" id="{hunk_id}"><header>{header}</header>'
                         f'<pre translate="no" aria-label="{patch_label}">{body}</pre></section>')
        body = "".join(hunks) or f'<p>{text("No text patch.")}</p>'
        if len(selected) < len(indexed):
            body += '<p class="meta">%s</p>' % (
                text("%d more hunks. Use Git for the full diff.") % (len(indexed) - len(selected)))
        panels.append(f'<details class="file" id="{file_id}"><summary><code translate="no">{label}</code>'
                      f'<span class="meta">{counts}</span></summary>{body}</details>')
    return "".join(nav), "".join(panels), locations


def report_inline(text, locations):
    parts = re.split(r'(`[^`\n]+`|\*\*[^*\n]+\*\*)', text)
    for i, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            value = part[1:-1]
            code = '<code translate="no">' + escape(value) + "</code>"
            match = re.fullmatch(r"(.+?):(\d+)(?:-\d+)?", value)
            path = match[1] if match else value
            if path in locations:
                file_id, lines = locations[path]
                candidates = lines.get(int(match[2]), []) if match else []
                # Prefer new line numbers and the latest matching hunk.
                target = max(candidates)[2] if candidates else file_id
                code = f'<a href="#{target}">{code}</a>'
            parts[i] = code
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            parts[i] = "<strong>" + escape(part[2:-2]) + "</strong>"
        else:
            parts[i] = escape(part)
    return "".join(parts)


def render_diagram(kind, source, inline, text, ui_attr):
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
        direction = ('<span class="connector">' + inline(label)
                     + f'<span class="arrow" aria-label="{text("Points to")}"{ui_attr}>→</span></span>')
        return '<li class="edge">' + start + direction + '<span class="node">' + inline(target) + '</span></li>'

    if kind in ("architecture", "flow"):
        groups = {}
        for origin, target, label in edges:
            groups.setdefault(origin, []).append(edge("", target, label))
        source = '<h3>{}</h3>' if kind == "architecture" else '<span class="node">{}</span>'
        body = '<ul class="groups">' + "".join(
            '<li class="group">' + source.format(inline(origin)) + '<ul>' + "".join(items) + '</ul></li>'
            for origin, items in groups.items()) + '</ul>'
    else:
        body = '<ol>' + "".join(edge(*item) for item in edges) + '</ol>'
    caption = {"flow": "Flowchart", "sequence": "Sequence diagram", "architecture": "Dependency diagram"}[kind]
    variant = "flowchart" if kind == "flow" else kind
    return f'<figure class="diagram {variant}"><figcaption{ui_attr}>{text(caption)}</figcaption>{body}</figure>'


def render_report(report, locations, text, ui_attr):
    """Render the report's Markdown subset, escaping raw HTML."""
    def inline(value):
        return report_inline(value, locations)

    blocks, headings = [], []
    for i, part in enumerate(re.split(r"(?m)^```([^\n]*)\n(.*?)^```[ \t]*$", report, flags=re.S), 0):
        if i % 3 == 1:
            language = part.strip()
        elif i % 3 == 2:
            if language in ("flow", "sequence", "architecture"):
                blocks.append(render_diagram(language, part, inline, text, ui_attr))
            elif language == "mermaid":
                blocks.append(f'<details><summary{ui_attr}>{text("Mermaid source")}</summary>'
                              + '<pre translate="no">' + escape(part) + "</pre></details>")
            elif language == "diff":
                rows = []
                for line in part.splitlines():
                    kind = {"+": "add", "-": "del"}.get(line[:1], "")
                    rows.append(f'<span class="line {kind}">{escape(line)}</span>')
                blocks.append('<pre translate="no"><code>' + "".join(rows) + "</code></pre>")
            else:
                blocks.append('<pre translate="no"><code>' + escape(part) + "</code></pre>")
        else:
            for paragraph in re.split(r"\n[ \t]*\n", part.strip()):
                lines = paragraph.splitlines()
                if not lines:
                    continue
                heading = re.match(r"^#{1,6} (.+)$", lines[0])
                if heading:
                    section_id = "section-%d" % (len(headings) + 1)
                    headings.append((section_id, re.sub(r"`|\*\*", "", heading[1])))
                    blocks.append(f'<h2 id="{section_id}">' + inline(heading[1]) + "</h2>")
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
    return "\n".join(blocks), headings


def review_page(title, branch, report, ledger, analysis, lang="en"):
    ui_language, text = report_locale(lang)
    ui_attr = f' lang="{ui_language}"' if ui_language.split("-")[0] != lang.lower().split("-")[0] else ""
    title_attr = ui_attr if not title else ""
    title = title or text("Change review")
    files = analysis["evidence"] if analysis else {}
    skipped = set(analysis["skipped"]) if analysis else set()
    file_nav, panels, locations = render_evidence(files, skipped, report_references(report), text)
    sections, headings = [], []
    facts = overview = ""
    scope = branch
    if analysis:
        scope += " · " + analysis["range"]
        added = sum(file_diff["add"] for file_diff in files.values())
        deleted = sum(file_diff["del"] for file_diff in files.values())
        file_count = text("%d file" if len(files) == 1 else "%d files") % len(files)
        facts = '<ul class="facts"%s><li>L%d</li><li>%s</li><li>+%d / −%d</li></ul>' % (
            ui_attr, analysis["level"], file_count, added, deleted)
        if len(analysis["entries"]) == 1:
            entry = analysis["entries"][0]
            fields = {"Intent": entry["fields"].get("Intent"), "Verification": " ｜ ".join(entry["verification_results"])}
            values = [f'<dt{ui_attr}>{text(key)}</dt><dd translate="no">{escape(value)}</dd>' for key, value in fields.items() if value]
            if values:
                overview = ('<details class="panel record-summary" id="record-summary">'
                            f'<summary{ui_attr}>{text("Recorded intent and verification")}</summary><dl>'
                            + "".join(values) + "</dl></details>")
    if report:
        content, headings = render_report(report, locations, text, ui_attr)
        sections.extend(headings or [("report", text("Review summary"))])
        report = '<section class="panel report" id="report">' + content + "</section>"
    evidence = ""
    if analysis:
        sections.append(("evidence", text("Code evidence")))
        back = f'<a href="#report">{text("Back to review")}</a>' if report else ""
        evidence = (f'<section class="panel" id="evidence"{ui_attr}><div class="evidence-heading"><h2>{text("Code evidence")}</h2>'
                    + back + '</div>') + (
            panels or f'<p>{text("No file changes in this scope.")}</p>') + "</section>"
    if overview:
        sections.append(("record-summary", text("Recorded intent and verification")))
    sections.append(("ledger-raw", text("Original change record")))
    authored = {section_id for section_id, _ in headings}
    nav = '<div class="nav-sections">' + "".join(
        '<a class="nav-section" href="#%s"%s>%s</a>' % (
            section_id, f' lang="{escape(lang)}"' if ui_attr and section_id in authored else "", escape(label))
        for section_id, label in sections
    ) + '</div>'
    if file_nav:
        filter_label = text("Filter files")
        nav += (f'<details class="file-nav" open><summary>{text("Browse files")}</summary>'
                f'<input id="file-filter" type="search" aria-label="{filter_label}" placeholder="{filter_label}" hidden>'
                f'<p id="file-filter-empty" class="meta" role="status" hidden>{text("No matching files")}</p>'
                + file_nav + "</details>")
    return VIEW_HTML.format(title=escape(title), scope=escape(scope), facts=facts, overview=overview,
                            nav=nav, report=report, evidence=evidence, ledger=escape(ledger), lang=escape(lang),
                            navigation=text("Report navigation"), record_label=text("Original change record"),
                            ui_attr=ui_attr, title_attr=title_attr)


def cmd_view(a):
    """Render offline HTML with the Git diff for the selected scope."""
    root = repo_root()
    br = current_branch()
    lp = ledger_path(root, br)
    if not os.path.exists(lp):
        die("No change record exists for the current branch.")
    report = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    analysis = None
    if a.scope or a.base:
        mode, ref = pick_mode(a, "entry")
        _, text = report_locale(a.lang)
        analysis = analyze(root, load_cfg(root), mode, ref, 0, a.current_entry, evidence=True, scope_text=text)
    out = page_path(root, br)
    with open(lp, encoding="utf-8") as ledger:
        page = review_page(a.title, br, report, ledger.read(), analysis, lang=a.lang)
    with open(out, "w", encoding="utf-8") as output:
        output.write(page)
    print("✓ %s" % os.path.relpath(out, root))
    if a.open:
        opener = "open" if sys.platform == "darwin" else ("start" if os.name == "nt" else "xdg-open")
        try:
            subprocess.run([opener, out], shell=(os.name == "nt"))
        except Exception:
            print("  Could not open the report automatically. Open the file above.")


def main():
    ap = argparse.ArgumentParser(prog="lw.py", description="ai-light-work branch-local change records")
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
                           help="Defaults to the latest entry. Use all/a or branch/b to widen the scope.")
        g.add_argument("--base", help="Compare the specified ref with the worktree.")
        p.add_argument("--current-entry", help="Label the current round when using --base.")
        g.add_argument("--branch", action="store_true", help="Full branch (merge base..worktree).")
        g.add_argument("--ledger", action="store_true", help="Change record (starting snapshot..worktree)")
        g.add_argument("--since-checkpoint", action="store_true", help="Last checkpoint..worktree")
        g.add_argument("--entry", help="All completed rounds of an entry, such as C-004.")
        if name == "score":
            g.add_argument("--latest-entry", action="store_true", help="The latest change record entry.")
        else:
            g.add_argument("--all-entries", action="store_true", help="All completed rounds of recorded entries.")
        p.add_argument("--hunks", type=int, default=nh)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
    p = sp.add_parser("snapshot", help="Capture the worktree before editing.")
    p.add_argument("--entry", help="Activate a round for an open change entry.")
    p.add_argument("--base", help="Reuse the original starting tree of an unfinished round.")
    p.set_defaults(fn=cmd_snapshot)
    p = sp.add_parser("checkpoint"); p.add_argument("--entry"); p.add_argument("--base"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_checkpoint)
    p = sp.add_parser("conflicts"); p.add_argument("--json", action="store_true")
    p.add_argument("--recorded-only", action="store_true", help="Only analyze checkpoints assigned to change entries.")
    p.set_defaults(fn=cmd_conflicts)
    sp.add_parser("lint").set_defaults(fn=cmd_lint)
    sp.add_parser("ask", help="Call before asking the user to resolve a conflict.").set_defaults(fn=cmd_ask)
    p = sp.add_parser("why", help="Find the change record for a line."); p.add_argument("targets", nargs="+", metavar="PATH:LINE")
    p.set_defaults(fn=cmd_why)
    p = sp.add_parser("backfill"); p.add_argument("--commit", default="HEAD"); p.set_defaults(fn=cmd_backfill)
    sp.add_parser("stats").set_defaults(fn=cmd_stats)
    p = sp.add_parser("view"); p.add_argument("--open", action="store_true")
    p.add_argument("--lang", type=language_tag, default="en", help="Report language tag (default: en).")
    p.add_argument("--title", help="Report title, such as Branch review or C-004 review."); p.set_defaults(fn=cmd_view)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--scope", type=parse_view_scope, metavar="SCOPE", help="Report scope: latest, C-xxx, all, or branch.")
    g.add_argument("--base", help="Starting snapshot for the current round.")
    p.add_argument("--current-entry", help="Label the current entry when using --base.")
    p = sp.add_parser("hook"); p.add_argument("event", choices=["session-start", "prompt", "pre-edit", "post-edit", "stop"])
    p.set_defaults(fn=cmd_hook)
    a = ap.parse_args()
    if getattr(a, "current_entry", None) and not a.base:
        ap.error("--current-entry requires --base")
    if not getattr(a, "fn", None):
        ap.print_help()
        return
    try:
        a.fn(a)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
