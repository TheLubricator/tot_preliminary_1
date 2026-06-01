"""
analyze_trees.py
================
Comprehensive analysis and correctness report for Game-of-24 tree JSONs.

Usage:
    python analyze_trees.py --tree_dir ./raw_tree --output report.txt

Features
--------
  1. ROBUST CORRECTNESS VERIFICATION
     - Uses state-chain arithmetic (not format_step / code parsing) so it
       succeeds even on the "problematic" trees that flagged_problematic_trees.txt
       lists (where extract_training_example returned None due to parse failures).
     - Also runs format_step verification to detect format-parse failures
       separately, flagging them as "correct-but-format-broken" rather than wrong.

  2. PER-DEPTH PROPOSAL ANALYSIS
     - For every depth d, reports:
         proposals_possible  = Σ n_select_sample over non-pruned, non-solution
                               parents at depth d-1  (what the tree *could* have made)
         proposals_made      = actual nodes at depth d
         pruned              = pruned nodes at depth d
         solutions           = solution nodes at depth d
         avg_value           = mean score of nodes at depth d
     - Depth 0 (root) is shown as a header row for reference.

  3. TREE-LEVEL STATISTICS
     Pulls directly from metadata.statistics:
       total_nodes, api_calls, solutions_found, code_executions, code_errors,
       deadend_memory_skipped, time_seconds, skip_rate, api_saved_*, tokens, etc.

  4. PROBLEMATIC TREE DETECTION & MITIGATION
     A tree is flagged PROBLEMATIC if any of the following:
       - (P1) solutions list is non-empty but state-chain verification fails
              for ALL solutions (correctness is uncertain).
       - (P2) solutions exist and state-chain passes but format_step parsing
              fails for all paths (example extraction would return None —
              the known issue from flagged_problematic_trees.txt).
       - (P3) metadata reports solutions_found > 0 but solutions list is empty
              (builder inconsistency).
     P2 trees are now counted as CORRECT by the robust verifier — this is the
     mitigation for the flagged trees.

  5. AGGREGATE SUMMARY
     - Overall correctness: X / Y trees solved correctly (state-chain verified).
     - P1 / P2 / P3 breakdown.
     - Depth-level aggregates across all trees.
     - Token, API, timing, deadend-memory aggregates.

Output: human-readable .txt report.
"""

import json
import math
import re
import sys
import itertools
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from datetime import datetime
import argparse
import statistics


# ═══════════════════════════════════════════════
#  ROBUST STATE-CHAIN SOLUTION VERIFIER
#  No code/format_step parsing — pure arithmetic
# ═══════════════════════════════════════════════

def _ops_on_pair(a: float, b: float) -> List[Tuple[str, float]]:
    """Return all valid (op_str, result) for operands a, b."""
    results = []
    results.append((f"{fmt(a)} + {fmt(b)}", a + b))
    results.append((f"{fmt(a)} - {fmt(b)}", a - b))
    results.append((f"{fmt(b)} - {fmt(a)}", b - a))
    results.append((f"{fmt(a)} * {fmt(b)}", a * b))
    if abs(b) > 1e-9:
        results.append((f"{fmt(a)} / {fmt(b)}", a / b))
    if abs(a) > 1e-9:
        results.append((f"{fmt(b)} / {fmt(a)}", b / a))
    return results


def fmt(x: float) -> str:
    if x == int(x):
        return str(int(x))
    return f"{x:.4g}"


def _state_transition_valid(parent_state: List[float],
                             child_state: List[float],
                             tol: float = 1e-4) -> bool:
    """
    Return True if child_state can be produced from parent_state by
    picking any two numbers, applying +/-/*/÷, and replacing them with
    the result.  Pure arithmetic — no code parsing.
    """
    ps = [float(x) for x in parent_state]
    cs = [float(x) for x in child_state]

    if len(cs) != len(ps) - 1:
        return False

    for i, j in itertools.combinations(range(len(ps)), 2):
        a, b = ps[i], ps[j]
        remaining = [ps[k] for k in range(len(ps)) if k not in (i, j)]
        for _, result in _ops_on_pair(a, b):
            candidate = sorted(remaining + [result])
            if _lists_close(candidate, sorted(cs), tol):
                return True
    return False


def _lists_close(a: List[float], b: List[float], tol: float = 1e-4) -> bool:
    if len(a) != len(b):
        return False
    return all(abs(x - y) < tol for x, y in zip(a, b))


def verify_solution_robust(nodes: Dict[int, dict],
                            sol_id: int) -> Tuple[bool, str]:
    """
    Walk the path root → sol_id, checking each state transition
    using pure arithmetic (no code parsing).

    Returns (is_valid, reason_if_invalid).
    """
    # Trace path root → leaf
    path = []
    node = nodes.get(sol_id)
    while node is not None:
        path.append(node)
        pid = node.get("parent_id")
        node = nodes.get(pid) if pid is not None else None
    path.reverse()  # root first

    if not path:
        return False, "empty path"

    # Final node must be a solution with state containing only 24
    final = path[-1]
    fs = [float(x) for x in final["state"]]
    if not (len(fs) == 1 and abs(fs[0] - 24.0) < 1e-4):
        return False, f"final state not [24]: {fs}"

    # Verify every transition
    for k in range(1, len(path)):
        parent_state = [float(x) for x in path[k - 1]["state"]]
        child_state  = [float(x) for x in path[k]["state"]]
        if not _state_transition_valid(parent_state, child_state):
            return False, (
                f"invalid transition at depth {path[k]['depth']}: "
                f"{parent_state} → {child_state}"
            )

    return True, "ok"


# ═══════════════════════════════════════════════
#  FORMAT-STEP VERIFICATION (mirrors extract_training_example)
#  Used to detect P2 (correct but format-broken) trees
# ═══════════════════════════════════════════════

def _format_step_parseable(node: dict) -> bool:
    """Return True if format_step would succeed for this node."""
    code        = node.get("codeact", {}).get("code", "")
    observation = node.get("codeact", {}).get("observation", "").strip()
    state       = node.get("state", [])

    # Try all three parse priorities
    match = re.search(
        r'(?:res|result)\s*=\s*(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)', code
    )
    if not match:
        match = re.search(
            r'#\s*(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)', code
        )
    if not match:
        nums_decl = re.search(r'numbers\s*=\s*\[([^\]]+)\]', code)
        idx_op    = re.search(
            r'(?:res|result)\s*=\s*numbers\[(\d+)\]\s*([+\-*/])\s*numbers\[(\d+)\]', code
        )
        if nums_decl and idx_op:
            return True   # Priority 3 would succeed
        return False

    if not match:
        return False

    a, b_val = float(match.group(1)), float(match.group(3))
    op = match.group(2)
    ops = {'+': lambda x,y: x+y, '-': lambda x,y: x-y,
           '*': lambda x,y: x*y, '/': lambda x,y: x/y}
    try:
        result = ops[op](a, b_val)
    except ZeroDivisionError:
        return False

    obs_numbers = [float(x) for x in re.findall(r'-?\d+\.?\d*', observation)]
    if not any(abs(x - result) < 1e-6 for x in obs_numbers):
        return False

    state_floats = [float(x) for x in state]
    if not any(math.isclose(x, result, abs_tol=1e-3, rel_tol=1e-3)
               for x in state_floats):
        return False

    return True


# ═══════════════════════════════════════════════
#  BRUTE-FORCE GAME-OF-24 SOLVER
#  Checks whether a given state (list of numbers) can reach 24.
#  Used to label nodes as "correct" independently of whether the
#  tree builder found a solution — fixing the case where the builder
#  failed despite a solvable path existing in the tree.
# ═══════════════════════════════════════════════

def _possible_results(a: float, b: float) -> List[float]:
    results = [a + b, a - b, b - a, a * b]
    if abs(b) > 1e-9:
        results.append(a / b)
    if abs(a) > 1e-9:
        results.append(b / a)
    return results


def can_reach_24(numbers: List[float], target: float = 24.0, tol: float = 1e-6) -> bool:
    """Return True if `numbers` can produce `target` using +/-/*/÷ (each number once)."""
    nums = [float(x) for x in numbers]
    if len(nums) == 1:
        return abs(nums[0] - target) < tol
    for i, j in itertools.combinations(range(len(nums)), 2):
        a, b = nums[i], nums[j]
        rest = [nums[k] for k in range(len(nums)) if k not in (i, j)]
        for r in _possible_results(a, b):
            if can_reach_24(rest + [r], target, tol):
                return True
    return False


def collect_winning_node_ids(nodes: Dict[int, dict],
                             solution_ids: List[int],
                             verified_only: bool = True) -> set:
    """
    Return the set of node IDs whose state can reach 24 (via brute-force solver).

    This is intentionally independent of `solution_ids` — it catches nodes that
    are on a valid path even when the tree builder failed to find the solution
    (e.g. the builder scored the node but then mis-expanded or exhausted its budget).

    `solution_ids` and `verified_only` are kept as parameters for API compatibility
    but are no longer used for the winning-node computation.
    """
    winning_ids = set()
    for nid, node in nodes.items():
        state = [float(x) for x in node.get("state", [])]
        if not state:
            continue
        # Root (depth 0) is always reachable; skip it (depth > 0 is meaningful)
        if node.get("depth", 0) == 0:
            continue
        if can_reach_24(state):
            winning_ids.add(nid)
    return winning_ids


def check_format_step_on_path(nodes: Dict[int, dict], sol_id: int) -> bool:
    """Return True if every non-root node on the solution path is format_step parseable."""
    path = []
    node = nodes.get(sol_id)
    while node is not None:
        path.append(node)
        pid = node.get("parent_id")
        node = nodes.get(pid) if pid is not None else None
    path.reverse()

    for n in path[1:]:  # skip root
        if not _format_step_parseable(n):
            return False
    return True


# ═══════════════════════════════════════════════
#  PER-DEPTH ANALYSIS
# ═══════════════════════════════════════════════

def compute_depth_stats(nodes_list: List[dict],
                        n_select_sample: int,
                        winning_node_ids: set = None) -> Dict[int, dict]:
    """
    For each depth, compute:
      - total_nodes          : nodes at this depth
      - pruned               : pruned nodes
      - solutions            : solution nodes
      - proposals_possible   : n_select_sample × qualifying parents at depth-1
      - proposals_made       : total_nodes
      - correct_proposed     : nodes at this depth that are on a verified solution path
      - correct_parent_had_it: whether the parent of the correct-path node at this
                               depth was itself on the winning path (funnel continuity)
      - avg_value            : mean value score
    """
    if winning_node_ids is None:
        winning_node_ids = set()

    depth_map: Dict[int, List[dict]] = {}
    for n in nodes_list:
        d = n["depth"]
        depth_map.setdefault(d, []).append(n)

    max_depth = max(depth_map.keys()) if depth_map else 0

    stats = {}
    for d in range(0, max_depth + 1):
        dnodes   = depth_map.get(d, [])
        pruned   = sum(1 for n in dnodes if n.get("is_pruned", False))
        solutions = sum(1 for n in dnodes if n.get("is_solution", False))
        values   = [n.get("value", 0.0) for n in dnodes if n.get("value") is not None]
        avg_val  = statistics.mean(values) if values else 0.0

        # How many proposals at this depth were on the winning path
        correct_proposed = sum(1 for n in dnodes if n["id"] in winning_node_ids)

        if d == 0:
            proposals_possible = 0
            proposals_made     = 0
        else:
            parents = depth_map.get(d - 1, [])
            qualifying = [p for p in parents
                          if not p.get("is_pruned", False)
                          and not p.get("is_solution", False)]
            proposals_possible = len(qualifying) * n_select_sample
            proposals_made     = len(dnodes)

        stats[d] = {
            "total_nodes":        len(dnodes),
            "pruned":             pruned,
            "solutions":          solutions,
            "proposals_possible": proposals_possible,
            "proposals_made":     proposals_made,
            "correct_proposed":   correct_proposed,   # ← correct steps at this depth
            "avg_value":          avg_val,
        }

    return stats


# ═══════════════════════════════════════════════
#  SINGLE-TREE ANALYSER
# ═══════════════════════════════════════════════

def analyze_tree(tree: dict, filename: str) -> dict:
    """
    Full analysis of one tree.  Returns a structured result dict.
    """
    meta  = tree.get("metadata", {})
    stats = meta.get("statistics", {})
    params = meta.get("parameters", {})

    nodes_list   = tree.get("nodes", [])
    solution_ids = tree.get("solutions", [])
    nodes        = {n["id"]: n for n in nodes_list}

    n_select_sample = params.get("n_select_sample", 5)

    # ── Correctness verification ──────────────────────────────────────
    chain_results = []   # (sol_id, chain_ok, chain_reason, fmt_ok)
    for sid in solution_ids:
        chain_ok, chain_reason = verify_solution_robust(nodes, sid)
        fmt_ok  = check_format_step_on_path(nodes, sid) if chain_ok else False
        chain_results.append((sid, chain_ok, chain_reason, fmt_ok))

    any_chain_ok = any(r[1] for r in chain_results)
    all_fmt_ok   = any(r[3] for r in chain_results if r[1])  # any fmt-ok among chain-ok

    # ── Problematic flags ─────────────────────────────────────────────
    flags = []
    # P1: solutions present but ALL fail state-chain verification
    if solution_ids and not any_chain_ok:
        flags.append("P1:chain-verify-failed")
    # P2: solutions exist, chain passes, but format_step would fail for all
    if solution_ids and any_chain_ok and not all_fmt_ok:
        flags.append("P2:format-parse-broken(correct-but-unextractable)")
    # P3: metadata says solutions_found > 0 but solutions list is empty
    meta_sol = stats.get("solutions_found", 0)
    if meta_sol > 0 and not solution_ids:
        flags.append("P3:metadata-solution-count-mismatch")

    # ── Winning-path node IDs (for correct-step tracking) ────────────
    winning_node_ids = collect_winning_node_ids(nodes, solution_ids, verified_only=True)

    # Per-depth funnel: at each depth, did this tree have the correct step?
    # (i.e., was there at least one winning-path node at that depth?)
    # Also track: how many trees first "lost" the correct path at each depth.

    # ── Per-depth stats ───────────────────────────────────────────────
    depth_stats = compute_depth_stats(nodes_list, n_select_sample, winning_node_ids)

    # ── Metadata stats collection ─────────────────────────────────────
    dm_summary = meta.get("deadend_memory_summary", {})
    dm_stats   = stats.get("deadend_memory", {})

    result = {
        "filename": filename,

        # Correctness
        "solutions_in_list":    len(solution_ids),
        "solutions_chain_ok":   sum(1 for r in chain_results if r[1]),
        "solutions_fmt_ok":     sum(1 for r in chain_results if r[1] and r[3]),
        "is_correct":           any_chain_ok,      # robust correctness (state-chain)
        "is_extractable":       any_chain_ok and all_fmt_ok,
        "chain_failures":       [(r[0], r[2]) for r in chain_results if not r[1]],

        # Flags
        "flags": flags,

        # Metadata stats
        "total_nodes":          stats.get("total_nodes",              len(nodes_list)),
        "api_calls":            stats.get("api_calls",                0),
        "cache_hits":           stats.get("cache_hits",               0),
        "solutions_found_meta": stats.get("solutions_found",          0),
        "code_executions":      stats.get("code_executions",          0),
        "code_errors":          stats.get("code_errors",              0),
        "time_seconds":         stats.get("time_seconds",             0.0),
        "total_input_tokens":   stats.get("total_input_tokens",       0),
        "total_output_tokens":  stats.get("total_output_tokens",      0),
        "deadend_enabled":      stats.get("deadend_memory_enabled",   False),
        "deadend_skipped":      stats.get("deadend_memory_skipped",   0),
        "api_saved_two_num":    stats.get("api_saved_two_num_cache",  0),
        "api_saved_3num":       stats.get("api_saved_3num_pattern",   0),
        "api_saved_value":      stats.get("api_saved_value_cache",    0),
        "skip_rate":            dm_stats.get("skip_rate",             "N/A"),
        "patterns_stored":      dm_stats.get("patterns_stored",
                                dm_summary.get("patterns_stored",    0)),
        "total_checked":        dm_stats.get("total_checked",
                                dm_summary.get("total_checked",      0)),

        # Proposal tracking
        "winning_depth1_count":    stats.get("winning_depth1_count",    0),
        "winning_depth1_proposed": stats.get("winning_depth1_proposed", 0),
        "correct_path_proposed":   stats.get("correct_path_in_proposals", False),

        # n_select_sample (for depth analysis context)
        "n_select_sample":      n_select_sample,
        "n_evaluate_sample":    params.get("n_evaluate_sample", 3),
        "max_steps":            params.get("max_steps", 6),

        # Per-depth breakdown
        "depth_stats": depth_stats,

        # Correct-step funnel: for each depth, was ≥1 winning-path node present?
        # key = depth (int), value = bool
        "correct_at_depth": {
            d: (ds["correct_proposed"] > 0)
            for d, ds in depth_stats.items()
            if d > 0  # exclude root
        },
    }

    return result


# ═══════════════════════════════════════════════
#  REPORT FORMATTER
# ═══════════════════════════════════════════════

SEP  = "═" * 80
SEP2 = "─" * 80
SEP3 = "·" * 80


def _pct(num, den):
    if den == 0:
        return "N/A"
    return f"{100.0 * num / den:.1f}%"


def format_report(results: List[dict]) -> str:
    lines = []

    def add(s=""):
        lines.append(s)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add(SEP)
    add(f"  GAME-OF-24 TREE ANALYSIS REPORT")
    add(f"  Generated: {now}")
    add(f"  Trees analysed: {len(results)}")
    add(SEP)

    # ── 1. OVERALL CORRECTNESS ────────────────────────────────────────
    add()
    add("┌─ 1. OVERALL CORRECTNESS (state-chain verified) ─────────────────────────────┐")
    add()

    total          = len(results)
    with_sol_list  = sum(1 for r in results if r["solutions_in_list"] > 0)
    correct        = sum(1 for r in results if r["is_correct"])
    extractable    = sum(1 for r in results if r["is_extractable"])
    no_sol         = total - with_sol_list
    p1_count       = sum(1 for r in results if "P1:chain-verify-failed" in r["flags"])
    p2_count       = sum(1 for r in results if any("P2:" in f for f in r["flags"]))
    p3_count       = sum(1 for r in results if "P3:metadata-solution-count-mismatch" in r["flags"])

    add(f"  Total trees                          : {total}")
    add(f"  Trees with solutions (list non-empty): {with_sol_list}")
    add(f"  Trees with NO solutions              : {no_sol}")
    add()
    add(f"  ✔ CORRECT  (state-chain verified)    : {correct} / {total}  ({_pct(correct, total)})")
    add(f"    of which extractable (fmt parseable): {extractable} / {correct}  ({_pct(extractable, correct)})")
    add()
    add(f"  ✘ P1 — chain-verify failed           : {p1_count}  (solutions present but arithmetic wrong)")
    add(f"  ⚠ P2 — correct but unextractable      : {p2_count}  (format_step parse failed; mitigated here)")
    add(f"  ⚠ P3 — metadata/list mismatch         : {p3_count}")
    add()
    add("└──────────────────────────────────────────────────────────────────────────────┘")

    # ── 2. AGGREGATE METADATA STATS ───────────────────────────────────
    add()
    add("┌─ 2. AGGREGATE METADATA STATISTICS ──────────────────────────────────────────┐")
    add()

    def agg(key):
        vals = [r[key] for r in results if isinstance(r.get(key), (int, float))]
        return vals

    def safe_avg(vals):
        return f"{statistics.mean(vals):.2f}" if vals else "N/A"

    def safe_sum(vals):
        return sum(vals) if vals else 0

    total_nodes_vals = agg("total_nodes")
    api_calls_vals   = agg("api_calls")
    time_vals        = agg("time_seconds")
    input_tok_vals   = agg("total_input_tokens")
    output_tok_vals  = agg("total_output_tokens")
    de_skipped_vals  = agg("deadend_skipped")
    patterns_vals    = agg("patterns_stored")

    add(f"  {'Metric':<40}  {'Total':>12}  {'Avg/tree':>12}")
    add(f"  {SEP2[:72]}")
    add(f"  {'Total nodes':<40}  {safe_sum(total_nodes_vals):>12}  {safe_avg(total_nodes_vals):>12}")
    add(f"  {'API calls':<40}  {safe_sum(api_calls_vals):>12}  {safe_avg(api_calls_vals):>12}")
    add(f"  {'Time (seconds)':<40}  {safe_sum(time_vals):>12.1f}  {safe_avg(time_vals):>12}")
    add(f"  {'Input tokens':<40}  {safe_sum(input_tok_vals):>12}  {safe_avg(input_tok_vals):>12}")
    add(f"  {'Output tokens':<40}  {safe_sum(output_tok_vals):>12}  {safe_avg(output_tok_vals):>12}")
    add(f"  {'Dead-end memory skipped':<40}  {safe_sum(de_skipped_vals):>12}  {safe_avg(de_skipped_vals):>12}")
    add(f"  {'Patterns stored (last tree avg)':<40}  {'N/A':>12}  {safe_avg(patterns_vals):>12}")

    # Winning depth-1 stats
    wd1_count    = safe_sum(agg("winning_depth1_count"))
    wd1_proposed = safe_sum(agg("winning_depth1_proposed"))
    correct_path = sum(1 for r in results if r.get("correct_path_proposed"))
    add()
    add(f"  Winning depth-1 moves (total across trees):")
    add(f"    Proposed that were on winning path : {wd1_count}")
    add(f"    Total depth-1 proposals (sum)      : {wd1_proposed}")
    add(f"    Trees where correct path proposed  : {correct_path} / {total}  ({_pct(correct_path, total)})")
    add()
    add("└──────────────────────────────────────────────────────────────────────────────┘")

    # ── 3. AGGREGATE PER-DEPTH ANALYSIS ──────────────────────────────
    add()
    add("┌─ 3. AGGREGATE PER-DEPTH PROPOSAL ANALYSIS ──────────────────────────────────┐")
    add()
    add("  Aggregated across all trees.")
    add("  'Possible' = n_select_sample × non-pruned/non-solution parents at depth d-1.")
    add("  'Made'     = actual nodes created at depth d.")
    add("  'Efficiency' = Made / Possible (how fully the tree was expanded).")
    add()
    # Collect all depths
    all_depths = set()
    for r in results:
        all_depths.update(r["depth_stats"].keys())
    all_depths_sorted = sorted(all_depths)

    add(f"  {'Depth':>5}  {'Trees w/depth':>13}  {'Possible':>10}  {'Made':>8}  "
        f"{'Correct':>8}  {'Correct%':>9}  {'Pruned':>7}  {'Solutions':>10}  "
        f"{'Efficiency':>11}  {'Avg Value':>10}")
    add(f"  {'─'*5}  {'─'*13}  {'─'*10}  {'─'*8}  "
        f"{'─'*8}  {'─'*9}  {'─'*7}  {'─'*10}  {'─'*11}  {'─'*10}")

    for d in all_depths_sorted:
        trees_with_depth = [r for r in results if d in r["depth_stats"]]
        if d == 0:
            total_nodes_d = sum(r["depth_stats"][d]["total_nodes"] for r in trees_with_depth)
            add(f"  {d:>5}  {len(trees_with_depth):>13}  {'(root)':>10}  {total_nodes_d:>8}  "
                f"{'—':>8}  {'—':>9}  {'—':>7}  {'—':>10}  {'N/A':>11}  {'N/A':>10}")
            continue

        possible_sum = sum(r["depth_stats"][d]["proposals_possible"] for r in trees_with_depth)
        made_sum     = sum(r["depth_stats"][d]["proposals_made"]     for r in trees_with_depth)
        correct_sum  = sum(r["depth_stats"][d]["correct_proposed"]   for r in trees_with_depth)
        pruned_sum   = sum(r["depth_stats"][d]["pruned"]             for r in trees_with_depth)
        sol_sum      = sum(r["depth_stats"][d]["solutions"]          for r in trees_with_depth)
        val_lists    = [r["depth_stats"][d]["avg_value"]             for r in trees_with_depth
                        if r["depth_stats"][d]["avg_value"] is not None]
        avg_val      = statistics.mean(val_lists) if val_lists else 0.0

        add(f"  {d:>5}  {len(trees_with_depth):>13}  {possible_sum:>10}  {made_sum:>8}  "
            f"{correct_sum:>8}  {_pct(correct_sum, made_sum):>9}  {pruned_sum:>7}  "
            f"{sol_sum:>10}  {_pct(made_sum, possible_sum):>11}  {avg_val:>10.3f}")

    add()
    add("  Note: 'Correct' = proposals whose state can reach 24 (brute-force solver).")
    add("  'Correct%' = Correct / Made  (what fraction of proposals were the right move).")
    add("  This is independent of whether the builder found the solution.")
    add()
    add("└──────────────────────────────────────────────────────────────────────────────┘")

    # ── 3b. CORRECT-STEP FUNNEL ───────────────────────────────────────
    add()
    add("┌─ 3b. CORRECT-STEP FUNNEL (per tree, per depth) ─────────────────────────────┐")
    add()
    add("  For each depth: how many trees had the correct step proposed at that depth.")
    add("  A tree 'has correct step at depth d' if ≥1 node at depth d has a state that")
    add("  can reach 24 (brute-force verified) — regardless of whether the builder")
    add("  actually found the solution.  This catches builder failures too.")
    add("  Read this as: of all trees, how many still had the winning path alive at d?")
    add()

    non_d = [d for d in all_depths_sorted if d > 0]
    add(f"  {'Depth':>5}  {'Trees w/correct step':>22}  {'/ Total':>8}  {'Fraction':>9}  {'Bar'}")
    add(f"  {'─'*5}  {'─'*22}  {'─'*8}  {'─'*9}  {'─'*30}")

    for d in non_d:
        trees_with_correct = sum(
            1 for r in results
            if r.get("correct_at_depth", {}).get(d, False)
        )
        frac = trees_with_correct / total if total else 0.0
        bar  = "█" * int(frac * 30)
        add(f"  {d:>5}  {trees_with_correct:>22}  {total:>8}  {frac:>9.3f}  {bar}")

    # "Correct" row — trees that actually reached a solution
    frac_correct = correct / total if total else 0.0
    bar_correct  = "█" * int(frac_correct * 30)
    add(f"  {'Correct':>5}  {correct:>22}  {total:>8}  {frac_correct:>9.3f}  {bar_correct}")
    add()
    add("└──────────────────────────────────────────────────────────────────────────────┘")

    # ── 4. PER-TREE DETAIL ────────────────────────────────────────────
    add()
    add("┌─ 4. PER-TREE DETAIL ─────────────────────────────────────────────────────────┐")
    add()

    for idx, r in enumerate(results, 1):
        status = "✔ CORRECT" if r["is_correct"] else "✘ NO SOLUTION"
        if r["is_correct"] and not r["is_extractable"]:
            status = "✔ CORRECT (P2-unextractable)"
        if r["flags"] and "P1:" in " ".join(r["flags"]):
            status = "✘ P1-CHAIN-FAIL"
        if r["flags"] and "P3:" in " ".join(r["flags"]):
            status += " [P3]"

        add(f"  [{idx:>3}]  {r['filename']}")
        add(f"         Status  : {status}")
        if r["flags"]:
            add(f"         Flags   : {', '.join(r['flags'])}")
        add(f"         Solutions: {r['solutions_in_list']} in list  |  "
            f"chain-ok: {r['solutions_chain_ok']}  |  fmt-ok: {r['solutions_fmt_ok']}")
        add(f"         Nodes   : {r['total_nodes']}  |  API calls: {r['api_calls']}  |  "
            f"Time: {r['time_seconds']:.1f}s")
        add(f"         Tokens  : in={r['total_input_tokens']}  out={r['total_output_tokens']}")
        add(f"         Deadend : skipped={r['deadend_skipped']}  skip_rate={r['skip_rate']}  "
            f"patterns={r['patterns_stored']}")
        add(f"         Params  : n_select={r['n_select_sample']}  n_eval={r['n_evaluate_sample']}  "
            f"max_steps={r['max_steps']}")
        if r["chain_failures"]:
            for sid, reason in r["chain_failures"]:
                add(f"         Chain fail  sol_id={sid}: {reason}")

        # Per-depth table for this tree
        add()
        add(f"         Depth-by-depth proposals (n_select_sample={r['n_select_sample']}):")
        add(f"         {'Depth':>5}  {'Possible':>9}  {'Made':>6}  {'Correct':>8}  "
            f"{'Correct%':>9}  {'Pruned':>7}  {'Solutions':>9}  {'AvgVal':>7}")
        add(f"         {'─'*5}  {'─'*9}  {'─'*6}  {'─'*8}  {'─'*9}  {'─'*7}  {'─'*9}  {'─'*7}")

        for d, ds in sorted(r["depth_stats"].items()):
            if d == 0:
                add(f"         {d:>5}  {'(root)':>9}  {ds['total_nodes']:>6}  "
                    f"{'—':>8}  {'—':>9}  {'—':>7}  {'—':>9}  {'N/A':>7}")
            else:
                eff      = _pct(ds["proposals_made"], ds["proposals_possible"])
                cor_pct  = _pct(ds["correct_proposed"], ds["proposals_made"])
                add(f"         {d:>5}  {ds['proposals_possible']:>9}  {ds['proposals_made']:>6}  "
                    f"{ds['correct_proposed']:>8}  {cor_pct:>9}  {ds['pruned']:>7}  "
                    f"{ds['solutions']:>9}  {ds['avg_value']:>7.3f}")

        add()
        add(f"         {SEP3[:72]}")
        add()

    add("└──────────────────────────────────────────────────────────────────────────────┘")

    # ── 5. FLAGGED PROBLEMATIC TREES SUMMARY ─────────────────────────
    add()
    add("┌─ 5. FLAGGED PROBLEMATIC TREES ──────────────────────────────────────────────┐")
    add()

    flagged = [r for r in results if r["flags"]]
    if not flagged:
        add("  None — all trees passed all checks.")
    else:
        add(f"  Total flagged: {len(flagged)}")
        add()
        add(f"  {'Flag':<48}  {'File'}")
        add(f"  {'─'*48}  {'─'*30}")
        for r in flagged:
            for fl in r["flags"]:
                add(f"  {fl:<48}  {r['filename']}")
        add()
        add("  Flag legend:")
        add("    P1: chain-verify-failed          — solutions present but state arithmetic fails.")
        add("    P2: format-parse-broken          — correct solution, but extract_training_example")
        add("                                       would return None (format_step parse error).")
        add("                                       These ARE counted as CORRECT in this report.")
        add("    P3: metadata-solution-count-mismatch — metadata says solved but list is empty.")
    add()
    add("└──────────────────────────────────────────────────────────────────────────────┘")

    add()
    add(SEP)
    add(f"  END OF REPORT  —  {len(results)} trees  —  {now}")
    add(SEP)

    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Analyse Game-of-24 tree JSONs and produce a correctness + stats report."
    )
    parser.add_argument(
        "--tree_dir", default=".",
        help="Directory containing game24_tree_*.json files (default: current dir)"
    )
    parser.add_argument(
        "--output", default="tree_analysis_report.txt",
        help="Output report file path (default: tree_analysis_report.txt)"
    )
    parser.add_argument(
        "--pattern", default="game24_tree_*.json",
        help="Glob pattern for tree files (default: game24_tree_*.json)"
    )
    args = parser.parse_args()

    tree_dir = Path(args.tree_dir)
    tree_files = sorted(tree_dir.glob(args.pattern))

    if not tree_files:
        print(f"[ERROR] No files matching '{args.pattern}' found in '{tree_dir}'.")
        sys.exit(1)

    print(f"Found {len(tree_files)} tree file(s).  Analysing...")

    results = []
    for i, tf in enumerate(tree_files, 1):
        print(f"  [{i:>3}/{len(tree_files)}]  {tf.name}", end="  ", flush=True)
        try:
            with open(tf, encoding="utf-8") as f:
                tree = json.load(f)
            result = analyze_tree(tree, tf.name)
            status = "✔" if result["is_correct"] else "✘"
            if result["flags"]:
                flags_short = ",".join(f.split(":")[0] for f in result["flags"])
                print(f"{status}  [{flags_short}]")
            else:
                print(status)
            results.append(result)
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({
                "filename": tf.name,
                "solutions_in_list": 0,
                "solutions_chain_ok": 0,
                "solutions_fmt_ok": 0,
                "is_correct": False,
                "is_extractable": False,
                "chain_failures": [],
                "flags": [f"LOAD_ERROR:{e}"],
                "total_nodes": 0,
                "api_calls": 0,
                "cache_hits": 0,
                "solutions_found_meta": 0,
                "code_executions": 0,
                "code_errors": 0,
                "time_seconds": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "deadend_enabled": False,
                "deadend_skipped": 0,
                "api_saved_two_num": 0,
                "api_saved_3num": 0,
                "api_saved_value": 0,
                "skip_rate": "N/A",
                "patterns_stored": 0,
                "total_checked": 0,
                "winning_depth1_count": 0,
                "winning_depth1_proposed": 0,
                "correct_path_proposed": False,
                "n_select_sample": 5,
                "n_evaluate_sample": 3,
                "max_steps": 6,
                "depth_stats": {},
            })

    report = format_report(results)

    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")
    print()
    print(f"Report written to: {output_path}")

    # Quick summary to stdout
    correct = sum(1 for r in results if r["is_correct"])
    total   = len(results)
    flagged = sum(1 for r in results if r["flags"])
    print(f"\nQuick summary: {correct}/{total} trees correct  |  {flagged} flagged")


if __name__ == "__main__":
    main()
