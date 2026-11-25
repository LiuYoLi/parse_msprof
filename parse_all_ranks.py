#!/usr/bin/env python3
"""
并行解析 Ascend Profile 多 Rank 数据。
- 默认跳过已解析（输出目录非空）的 PROF_*
- --rank / --force 触发强制重跑，且同样走并行
- 未指定 -j 时：强制模式默认 min(任务数, 8)，普通模式 min(任务数, 4)
- -j指定并发数
"""

import argparse
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


DEFAULT_MARKERS = [
    "mindstudio_profiler_output",
    "ASCEND_PROFILER_OUTPUT",
    "op_statistic.csv",
    "kernel_details.csv",
    "op_summary.csv",
]


def find_msprof() -> str:
    msprof = shutil.which("msprof")
    if msprof:
        return msprof
    candidates = [
        "/usr/local/Ascend/ascend-toolkit/latest/tools/profiler/bin/msprof",
        "/usr/local/Ascend/tools/profiler/bin/msprof",
        str(Path.home() / "Ascend/ascend-toolkit/latest/tools/profiler/bin/msprof"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    print("[ERROR] 未找到 msprof 命令，请确认 CANN Toolkit 环境已加载。", file=sys.stderr)
    print("        可尝试: source /usr/local/Ascend/ascend-toolkit/set_env.sh", file=sys.stderr)
    sys.exit(1)


def extract_rank(p: Path) -> Optional[int]:
    for part in p.parts:
        if part.startswith("rank_"):
            try:
                return int(part[len("rank_"):])
            except ValueError:
                return None
    return None


def parse_rank_spec(spec: str) -> set[int]:
    ranks: set[int] = set()
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            a, b = piece.split("-", 1)
            a, b = int(a.strip()), int(b.strip())
            if a > b:
                a, b = b, a
            ranks.update(range(a, b + 1))
        else:
            ranks.add(int(piece))
    return ranks


def discover_prof_dirs(root: Path) -> list[Path]:
    prof_dirs = [p for p in root.glob("rank_*/liteserver*_ascend_pt/PROF_*") if p.is_dir()]
    if not prof_dirs:
        prof_dirs = [p for p in root.rglob("PROF_*") if p.is_dir()]
    prof_dirs.sort(key=lambda p: (extract_rank(p) if extract_rank(p) is not None else 999,
                                   str(p)))
    return prof_dirs


def is_already_parsed(prof_dir: Path, markers: list[str]) -> tuple[bool, str]:
    for marker in markers:
        p = prof_dir / marker
        try:
            if p.is_dir():
                if any(p.iterdir()):
                    return True, f"{marker}/"
            elif p.is_file() and p.stat().st_size > 0:
                return True, marker
        except OSError:
            pass
    return False, ""


def parse_one(msprof: str, prof_dir: Path, dry_run: bool) -> dict:
    cmd = [msprof, "--export=on", f"--output={prof_dir}"]
    cmd_str = " ".join(cmd)
    start = time.time()
    if dry_run:
        return {"dir": prof_dir, "ok": True,
                "output": f"[DRY-RUN] {cmd_str}", "elapsed": 0.0}
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        return {"dir": prof_dir, "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "output": (proc.stdout or "").strip(),
                "elapsed": time.time() - start}
    except FileNotFoundError:
        return {"dir": prof_dir, "ok": False,
                "output": f"无法执行 msprof: {msprof}",
                "elapsed": time.time() - start}
    except Exception as e:
        return {"dir": prof_dir, "ok": False, "output": str(e),
                "elapsed": time.time() - start}


def _rank_label(p: Path) -> str:
    r = extract_rank(p)
    return f" [rank_{r}]" if r is not None else ""


def _print_result(res: dict, done: int, total: int) -> None:
    status = "OK" if res["ok"] else "FAIL"
    print(f"[{done}/{total}] {status}  {res['elapsed']:6.1f}s"
          f"{_rank_label(res['dir'])}  {res['dir']}")
    if not res["ok"] and res["output"]:
        tail = "\n".join(res["output"].splitlines()[-10:])
        if tail:
            print(f"    {tail}")


def main():
    parser = argparse.ArgumentParser(
        description="并行解析 Ascend Profile，支持跳过已解析 / 强制多 rank 并行重跑"
    )
    parser.add_argument("root", nargs="?", default="profile",
                        help="profile 根目录（相对路径），默认 ./profile")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="并发数。未指定时：强制模式用 min(任务数, 8)，"
                             "普通模式用 min(任务数, 4)；-j 1 强制串行")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要执行的命令，不实际解析")
    parser.add_argument("--force", action="store_true",
                        help="忽略已解析判断，全部强制重跑（并行）")
    parser.add_argument("--rank", default=None,
                        help="只处理指定 rank，如 5 / 5,6,7 / 5-8 / 5,7-9。"
                             "指定后默认强制重跑（并行）")
    parser.add_argument("--skip-parsed", action="store_true",
                        help="即使指定了 --rank，也跳过已解析的（去掉隐含的强制行为）")
    parser.add_argument("--marker", action="append", default=None,
                        help="追加自定义已解析标志（可多次），如 --marker xxx.csv")
    parser.add_argument("--msprof", default=None,
                        help="手动指定 msprof 路径（默认自动查找）")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[ERROR] 目录不存在（相对当前目录）: {root}", file=sys.stderr)
        sys.exit(1)

    markers = list(DEFAULT_MARKERS)
    if args.marker:
        markers.extend(args.marker)

    print(f"[INFO] 扫描目录: {root}  (cwd: {Path.cwd()})")
    prof_dirs = discover_prof_dirs(root)
    if not prof_dirs:
        print("[ERROR] 未找到任何 PROF_* 目录。", file=sys.stderr)
        sys.exit(1)

    # --rank 过滤
    rank_filter: Optional[set[int]] = None
    if args.rank:
        try:
            rank_filter = parse_rank_spec(args.rank)
        except ValueError:
            print(f"[ERROR] --rank 参数格式不正确: {args.rank}", file=sys.stderr)
            sys.exit(1)

        matched = [d for d in prof_dirs if extract_rank(d) in rank_filter]
        found = {extract_rank(d) for d in matched if extract_rank(d) is not None}
        missing = sorted(rank_filter - found)
        if missing:
            print(f"[WARN] 以下 rank 在目录中未找到，已忽略: {missing}", file=sys.stderr)
        if not matched:
            print("[ERROR] 没有任何目录匹配 --rank。", file=sys.stderr)
            sys.exit(1)
        prof_dirs = matched
        print(f"[INFO] --rank 过滤后 {len(prof_dirs)} 个 PROF_*: rank={sorted(found)}")

    # 强制判定
    force = args.force or (rank_filter is not None and not args.skip_parsed)

    # 分类
    parsed: list[tuple[Path, str]] = []
    pending: list[Path] = []
    for d in prof_dirs:
        if force:
            pending.append(d)
            continue
        ok, why = is_already_parsed(d, markers)
        if ok:
            parsed.append((d, why))
        else:
            pending.append(d)

    print(f"[INFO] 共 {len(prof_dirs)} 个 PROF_* 目录")
    print(f"[INFO] 已解析跳过: {len(parsed)}，待处理: {len(pending)}")
    if force:
        print(f"[INFO] 强制重跑模式已启用"
              + (f"（--rank {args.rank}）" if rank_filter is not None else "（--force）"))
    if parsed:
        print("[INFO] 跳过的目录（最多显示前20条）：")
        for d, why in parsed[:20]:
            print(f"  - {_rank_label(d).strip()} {d}  <- {why}")
        if len(parsed) > 20:
            print(f"  ... 其余 {len(parsed) - 20} 条省略")

    if not pending:
        print("\n[INFO] 没有需要处理的目录。")
        return

    # 并发数决策
    if args.jobs is not None:
        if args.jobs < 1:
            print("[ERROR] --jobs 必须 >= 1", file=sys.stderr)
            sys.exit(1)
        jobs = min(args.jobs, len(pending))
        jobs_reason = f"用户指定 -j {args.jobs}"
    elif force:
        jobs = min(len(pending), 8)
        jobs_reason = "强制模式默认 min(任务数, 8)"
    else:
        jobs = min(len(pending), 4)
        jobs_reason = "普通模式默认 min(任务数, 4)"

    msprof = args.msprof or find_msprof()
    print(f"\n[INFO] 使用 msprof: {msprof}")
    print(f"[INFO] 并发: jobs={jobs}（{jobs_reason}）"
          + ("  ⚠ 串行执行" if jobs == 1 else ""))
    if force and len(pending) > 1:
        print(f"[INFO] 并行强制重跑 {len(pending)} 个目录"
              + ("" if jobs > 1 else "（但并发=1，实为串行）"))
    print()

    success = 0
    failed: list[dict] = []
    done = 0
    total = len(pending)
    t0 = time.time()

    if jobs == 1:
        for d in pending:
            res = parse_one(msprof, d, args.dry_run)
            done += 1
            _print_result(res, done, total)
            if res["ok"]:
                success += 1
            else:
                failed.append(res)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(parse_one, msprof, d, args.dry_run): d
                       for d in pending}
            for fut in as_completed(futures):
                res = fut.result()
                done += 1
                _print_result(res, done, total)
                if res["ok"]:
                    success += 1
                else:
                    failed.append(res)

    elapsed = time.time() - t0
    print("=" * 60)
    print(f"汇总: 已跳过={len(parsed)}, 本次处理={total}, "
          f"成功={success}, 失败={len(failed)}, 耗时={elapsed:.1f}s")
    if failed:
        print("\n失败的目录:")
        for r in failed:
            print(f"  - {r['dir']}")
        sys.exit(1)
    else:
        print("全部完成。")


if __name__ == "__main__":
    main()
