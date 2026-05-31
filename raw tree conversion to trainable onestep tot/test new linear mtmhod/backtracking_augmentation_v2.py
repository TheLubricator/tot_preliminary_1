"""
backtracking_augmentation_v2.py
=================================
Rich backtracking linearizer that fixes the rigid 3-step-dead-end → root-backtrack → 3-step-solution
pattern from v1.

Root cause of v1 rigidity
--------------------------
v1 only produced depth-1 or depth-2 dead-end paths, always followed by a single
"Backtrack. (to: root)" and then a full 3-step solution.  The model learned a
deterministic macro: "Backtrack. = try one more 3-step root-level path" and looped
when that retry also failed.

What changed
------------
1. **Backtrack target is the actual branching point** (could be root OR an intermediate
   state like "12 5 9"), derived directly from the tree structure.

2. **Dead-end path length varies** (2–4 steps per detour) depending on how deep in
   the tree the branch was abandoned.

3. **Multiple backtracks per example** (1–4+) by collecting ALL dead-end branches
   that the tree explored before finding the solution, across ALL levels of the
   solution path.

4. **Same-level multi-backtrack** (Variation 3) is also supported: multiple
   dead-end siblings at the same branching point → multiple consecutive
   "Backtrack. (to: X)" lines pointing to the same X.

Target distribution
-------------------
  30%  zero backtracks  (clean examples — unchanged from v1)
  30%  one backtrack
  25%  two backtracks
  15%  three or more backtracks

Invariants preserved
--------------------
  - Arithmetic step format:  "A op B = R (left: X Y Z)"
  - Answer line format:       "Answer: <nested expr> = 24"
  - Backtrack marker format:  "Backtrack. (to: X Y Z)"
  - In-context header:        unchanged from v1
"""

import json
import math
import re
import random
from collections import defaultdict, Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from fractions import Fraction

# ── Imports from v1 ─────────────────────────────────────────────────────────

from extract_training_example import (
    format_step,
    fmt_num,
    trace_solution_path,
    build_final_expression,
    extract_training_example,
    _operands_in_state,
)
from backtracking_augmentation import (
    can_reach_24,
    _state_is_near_miss,
    PRUNED_VALUE_THRESHOLD,
    IN_CONTEXT_HEADER,
)

# ── Config ───────────────────────────────────────────────────────────────────

# Target fractions for each backtrack count bucket (0, 1, 2, 3+)
TARGET_DIST: Dict[int, float] = {0: 0.30, 1: 0.30, 2: 0.25, 3: 0.15}

# At most this many dead-end detours from a single branching point
MAX_DETOURS_PER_LEVEL = 2

# Hard cap on total backtracks in one example (keeps examples readable)
MAX_TOTAL_BACKTRACKS = 4


# ── Utilities ────────────────────────────────────────────────────────────────

def state_str(state: list) -> str:
    """Format a node state list as space-separated integers/decimals.

    Examples:
        [1.0, 3.0, 6] → "1 3 6"
        [0.125, 3]    → "0.125 3"
    """
    return " ".join(fmt_num(float(x)) for x in state)


def is_usable_dead_end(node: dict, solution_node_ids: set) -> bool:
    """Return True iff this tree node is a genuine dead-end usable for a detour.

    Conditions (all must hold):
      - Not on the current solution path
      - Not marked as a solution itself
      - Has parseable codeact.code (so format_step can produce a step line)
      - State cannot reach 24 (confirmed dead end)
      - State is not a near-miss (e.g. [24, 1])
      - Pruned by the tree builder, OR has value ≤ PRUNED_VALUE_THRESHOLD
    """
    if node["id"] in solution_node_ids:
        return False
    if node.get("is_solution", False):
        return False
    code = node.get("codeact", {}).get("code", "").strip()
    if not code:
        return False
    state = [float(x) for x in node.get("state", [])]
    if not state:
        return False
    if _state_is_near_miss(state):
        return False
    if can_reach_24(state):
        return False
    return node.get("is_pruned", False) or node.get("value", 1.0) <= PRUNED_VALUE_THRESHOLD


def is_terminating(f: Fraction) -> bool:
    """Check if fraction has a terminating decimal representation."""
    d = f.denominator
    while d % 2 == 0: d //= 2
    while d % 5 == 0: d //= 5
    return d == 1

def find_dead_end_extension(state, rng):
    nums = [float(x) for x in state]
    candidates = []

    for i in range(len(nums)):
        for j in range(len(nums)):
            if i == j:
                continue
            a, b = nums[i], nums[j]
            for op in ("+", "-", "*", "/"):
                try:
                    if op == "+":   res = a + b
                    elif op == "-": res = a - b
                    elif op == "*": res = a * b
                    else:
                        if abs(b) < 1e-12: continue
                        res = a / b
                except Exception:
                    continue

                # ── NEW: skip non-terminating division results ──────────
                if op == "/":
                    res_frac = Fraction(a) / Fraction(b)
                    if not is_terminating(res_frac):
                        continue   # don't use 8/3, 4/13, etc. in dead ends
                # ────────────────────────────────────────────────────────

                rest = [nums[k] for k in range(len(nums)) if k != i and k != j]
                new_state = [res] + rest

                if can_reach_24(new_state): continue
                if _state_is_near_miss(new_state): continue

                left_str = " ".join(fmt_num(x) for x in new_state)
                step_line = (
                    f"{fmt_num(a)} {op} {fmt_num(b)} = {fmt_num(res)}"
                    f" (left: {left_str})"
                )
                candidates.append((abs(res - 24.0), step_line, new_state))

    if not candidates:
        return None

    rng.shuffle(candidates)
    candidates.sort(key=lambda t: -t[0])
    _, step_line, new_state = candidates[0]
    return (step_line, new_state)


# ── Dead-end detour generator ────────────────────────────────────────────────

def generate_dead_end_detour(
    dead_end_node: dict,
    parent_state: list,
    n_extra_steps: int,
    rng: random.Random,
) -> Optional[List[str]]:
    """Generate the step lines for one dead-end detour.

    The detour starts at *dead_end_node* (a sibling of the next solution step,
    branching from the parent whose state is *parent_state*).  Up to
    *n_extra_steps* synthetic extension steps are appended to deepen the detour.

    Returns a list of step-line strings, or None if the first step cannot be
    formatted (corrupted node) or operand verification fails.

    Detour length: 1 + (0..n_extra_steps), depending on how far the extension
    can go without hitting a reachable-24 state.
    """
    # ── Format the first step (from the tree node itself) ──────────────────
    first_line = format_step(dead_end_node)
    if first_line is None:
        return None

    # Type-A operand check: operands must exist in parent state
    m = re.match(r"(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)", first_line)
    if m:
        a_val = float(m.group(1))
        b_val = float(m.group(2))
        if not _operands_in_state(a_val, b_val, parent_state):
            return None

    steps = [first_line]
    current_state = [float(x) for x in dead_end_node["state"]]

    # ── Extend synthetically ───────────────────────────────────────────────
    for _ in range(n_extra_steps):
        if len(current_state) <= 1:
            break  # Already terminal
        ext = find_dead_end_extension(current_state, rng)
        if ext is None:
            break  # No valid extension; accept shorter detour
        steps.append(ext[0])
        current_state = ext[1]

    return steps


# ── Tree-level analysis ──────────────────────────────────────────────────────

def pick_best_solution_path(
    nodes: dict, solution_ids: list
) -> Tuple[Optional[list], Optional[list]]:
    """Find the shortest valid solution path and its formatted step lines.

    Returns (path, steps) where path is root→leaf and steps are the formatted
    arithmetic lines, or (None, None) if no valid path exists.
    """
    best_path, best_steps = None, None

    for sol_id in solution_ids:
        path = trace_solution_path(nodes, sol_id)
        steps: List[str] = []
        valid = True

        for k, n in enumerate(path[1:], 1):
            line = format_step(n)
            if line is None:
                valid = False
                break
            m = re.match(r"(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)", line)
            if m:
                a_val, b_val = float(m.group(1)), float(m.group(2))
                if not _operands_in_state(a_val, b_val, path[k - 1]["state"]):
                    valid = False
                    break
            steps.append(line)

        if valid and steps:
            if best_path is None or len(steps) < len(best_steps):
                best_path, best_steps = path, steps

    return best_path, best_steps


def collect_dead_end_siblings_by_level(
    tree: dict,
    sol_path: list,
    solution_node_ids: set,
) -> Dict[int, List[dict]]:
    """For each level k, collect dead-end siblings of sol_path[k+1].

    Level k corresponds to the branching point at sol_path[k].
    The dead-end siblings are children of sol_path[k] that are NOT on the
    solution path and satisfy is_usable_dead_end().

    Returns: {level_k: [dead_end_node, ...]}

    Level 0 → dead-end siblings of the first solution step (tried from root)
    Level 1 → dead-end siblings of the second solution step (tried from state X)
    ...
    """
    result: Dict[int, List[dict]] = {}

    for k in range(len(sol_path) - 1):
        branching_node = sol_path[k]
        sol_child_id = sol_path[k + 1]["id"]

        siblings = [
            n
            for n in tree["nodes"]
            if n["parent_id"] == branching_node["id"]
            and n["id"] != sol_child_id
            and is_usable_dead_end(n, solution_node_ids)
        ]

        if siblings:
            result[k] = siblings

    return result


# ── n_extra assignment ────────────────────────────────────────────────────────

def choose_n_extra(dead_end_node: dict, rng: random.Random) -> int:
    """Choose how many extension steps to add after the dead-end node's own step.

    Extension budget depends on how many numbers remain in the dead-end state:
      ≥ 3 numbers remaining → extend by 1 or 2 steps (total 2–3 step detour)
      2 numbers remaining   → always extend by 1 step  (total 2-step detour,
                               terminal at 1 number — unambiguous dead end)
      1 number remaining    → no extension (already terminal)

    This ensures every detour terminates at 1 number, which gives the model the
    clearest "this path is dead" signal.
    """
    n_nums = len(dead_end_node.get("state", []))
    if n_nums >= 3:
        return rng.choice([1, 2])
    elif n_nums == 2:
        return 1  # always reach terminal
    else:
        return 0


# ── Rich example builder ─────────────────────────────────────────────────────

def build_rich_backtracking_example(
    tree: dict,
    sol_path: list,
    sol_steps: list,
    puzzle_str: str,
    # Each slot: (level_k, dead_end_node, n_extra_steps)
    selected_detours: List[Tuple[int, dict, int]],
    rng: random.Random,
) -> Optional[dict]:
    """Compose one training example with the given dead-end detour slots.

    Linearization order
    -------------------
    For k = 0, 1, …, len(sol_steps)-1:
        [detours at level k, each followed by "Backtrack. (to: <state_at_k>)"]
        <solution step k>
    Answer: <nested expression> = 24

    The "Backtrack. (to: X)" target is the state of sol_path[k], which is the
    actual branching point in the tree — not always the root.

    Returns a dict with puzzle/prompt/completion/metadata, or None on failure.
    """
    # Group selected detours by level
    detours_by_level: Dict[int, List[Tuple[dict, int]]] = defaultdict(list)
    for level_k, de_node, n_extra in selected_detours:
        detours_by_level[level_k].append((de_node, n_extra))

    completion_lines: List[str] = []
    n_backtracks = 0

    for k in range(len(sol_steps)):
        branching_node = sol_path[k]
        branch_target = state_str(branching_node["state"])

        # ── Insert detours at this level ───────────────────────────────────
        for de_node, n_extra in detours_by_level.get(k, []):
            detour_steps = generate_dead_end_detour(
                de_node, branching_node["state"], n_extra, rng
            )
            if detour_steps is None:
                return None  # Formatting failure; discard example

            completion_lines.extend(detour_steps)
            completion_lines.append(f"Backtrack. (to: {branch_target})")
            n_backtracks += 1

        # ── Append solution step ───────────────────────────────────────────
        completion_lines.append(sol_steps[k])

    if n_backtracks == 0:
        return None  # No detours were actually inserted

    final_expr = build_final_expression(sol_path, sol_steps)
    completion_lines.append(f"Answer: {final_expr}")

    # Build prompt (identical format to v1)
    prompt = (
        IN_CONTEXT_HEADER
        + f"Numbers: {puzzle_str}. Target: 24.\n"
        + "Use each number exactly once with +, -, *, / to reach 24.\n"
        + "Steps:"
    )

    # Collect which levels were used (for metadata)
    levels_used = sorted(detours_by_level.keys())

    return {
        "puzzle": puzzle_str,
        "prompt": prompt,
        "completion": "\n".join(completion_lines),
        "example_type": "backtracking",
        "n_backtracks": n_backtracks,
        "backtrack_levels": levels_used,
        "has_non_root_backtrack": any(k > 0 for k in levels_used),
        "has_deadend_ctx": False,
        "solution_steps": len(sol_steps),
    }


# ── Per-tree candidate generation ────────────────────────────────────────────

def generate_all_bt_candidates_for_tree(
    tree: dict,
    rng: random.Random,
) -> Dict[int, List[dict]]:
    """Generate all viable backtracking examples for one tree, grouped by n_backtracks.

    Strategy
    --------
    1. Find the shortest valid solution path.
    2. At each level k, collect up to MAX_DETOURS_PER_LEVEL dead-end siblings.
    3. Build an ordered pool of (level_k, de_node) slots.
    4. For each target backtrack count N in 1..MAX_TOTAL_BACKTRACKS:
         a. Select N slots from the pool, preferring cross-level spread.
         b. Try to build an example; on failure, try next candidate set.
    5. Return {n_bt: [example_dicts]}.

    The batch processor then samples from these candidates to hit TARGET_DIST.
    """
    nodes = {n["id"]: n for n in tree["nodes"]}
    solution_ids = tree.get("solutions", [])

    sol_path, sol_steps = pick_best_solution_path(nodes, solution_ids)
    if sol_path is None:
        return {}

    puzzle_str = " ".join(fmt_num(float(x)) for x in nodes[0]["state"])
    solution_node_ids = {n["id"] for n in sol_path}

    dead_ends_by_level = collect_dead_end_siblings_by_level(
        tree, sol_path, solution_node_ids
    )
    if not dead_ends_by_level:
        return {}

    # Build flat pool: up to MAX_DETOURS_PER_LEVEL slots per level, sorted by level
    pool: List[Tuple[int, dict]] = []  # (level_k, de_node)
    for level_k in sorted(dead_ends_by_level.keys()):
        candidates = list(dead_ends_by_level[level_k])
        rng.shuffle(candidates)
        for de_node in candidates[:MAX_DETOURS_PER_LEVEL]:
            pool.append((level_k, de_node))

    results: Dict[int, List[dict]] = defaultdict(list)

    for n_bt in range(1, MAX_TOTAL_BACKTRACKS + 1):
        if len(pool) < n_bt:
            continue

        # ── Select n_bt slots ─────────────────────────────────────────────
        # Priority: spread across as many distinct levels as possible, then
        # fill from same levels if more slots needed.
        unique_levels = sorted(set(lk for lk, _ in pool))

        selected_slots: List[Tuple[int, dict]] = []

        # First pass: one slot per level
        used_in_first_pass: set = set()
        for level in unique_levels:
            for slot in pool:
                if slot[0] == level and id(slot) not in used_in_first_pass:
                    selected_slots.append(slot)
                    used_in_first_pass.add(id(slot))
                    break
            if len(selected_slots) >= n_bt:
                break

        # Second pass: fill remaining from any level (duplicates allowed)
        if len(selected_slots) < n_bt:
            remaining = [s for s in pool if id(s) not in used_in_first_pass]
            for slot in remaining:
                if len(selected_slots) >= n_bt:
                    break
                selected_slots.append(slot)

        if len(selected_slots) < n_bt:
            continue

        # Ensure at most MAX_DETOURS_PER_LEVEL per level in final selection
        level_counts: Counter = Counter()
        final_slots: List[Tuple[int, dict]] = []
        for slot in selected_slots[:n_bt]:
            lk = slot[0]
            if level_counts[lk] < MAX_DETOURS_PER_LEVEL:
                final_slots.append(slot)
                level_counts[lk] += 1
        if len(final_slots) < n_bt:
            continue

        # Assign n_extra for each slot
        selected_detours = [
            (lk, de_node, choose_n_extra(de_node, rng))
            for (lk, de_node) in final_slots
        ]

        # Try to build the example
        ex = build_rich_backtracking_example(
            tree, sol_path, sol_steps, puzzle_str, selected_detours, rng
        )
        if ex is not None:
            # Count actual backtracks (some detours may have been skipped on None)
            bucket = min(ex["n_backtracks"], MAX_TOTAL_BACKTRACKS)
            results[bucket].append(ex)

    return dict(results)


# ── Batch processor ───────────────────────────────────────────────────────────

def generate_augmented_dataset_v2(
    tree_dir: str,
    dead_end_db_path: str,
    output_path: str = "training_data_augmented_v2.jsonl",
    seed: int = 42,
    flag_file: Optional[str] = "flagged_v2.txt",
) -> dict:
    """Process all tree JSONs and generate a mixed dataset with rich backtracks.

    Two-pass algorithm
    ------------------
    Pass 1: collect clean examples + all rich backtracking candidates per tree.
    Pass 2: sample from candidates to hit TARGET_DIST as closely as possible.

    Distribution targets (TARGET_DIST)::
        30%  → 0 backtracks (clean)
        30%  → 1 backtrack
        25%  → 2 backtracks
        15%  → 3+ backtracks
    """
    rng = random.Random(seed)

    with open(dead_end_db_path, encoding="utf-8") as f:
        dead_end_db = json.load(f)

    tree_files = sorted(Path(tree_dir).glob("game24_tree_*.json"))
    print(f"Found {len(tree_files)} tree files.")

    # ── Pass 1 ────────────────────────────────────────────────────────────
    clean_examples: List[dict] = []
    rich_pool: Dict[int, List[dict]] = defaultdict(list)
    skipped = 0
    no_bt_candidates = 0

    for tree_path in tree_files:
        with open(tree_path, encoding="utf-8") as f:
            tree = json.load(f)

        # Clean example
        clean = extract_training_example(tree, dead_end_db)
        if clean is None:
            skipped += 1
            continue

        clean["example_type"] = "clean"
        if not clean["prompt"].startswith("Here are some solved examples:"):
            clean["prompt"] = IN_CONTEXT_HEADER + clean["prompt"]
        clean_examples.append(clean)

        # Rich backtracking candidates
        bt_candidates = generate_all_bt_candidates_for_tree(tree, rng)
        if not bt_candidates:
            no_bt_candidates += 1
        for n_bt, exs in bt_candidates.items():
            rich_pool[n_bt].extend(exs)

    n_clean = len(clean_examples)
    if n_clean == 0:
        print("No valid clean examples found. Aborting.")
        return {}

    print(
        f"Clean: {n_clean} | Skipped: {skipped} | "
        f"Trees with no BT candidates: {no_bt_candidates}"
    )
    print(
        "Rich candidates by n_bt: "
        + str({k: len(v) for k, v in sorted(rich_pool.items())})
    )

    # ── Pass 2: sample to hit TARGET_DIST ────────────────────────────────
    # n_clean IS 30% → total target = n_clean / 0.30
    n_total_target = n_clean / TARGET_DIST[0]
    targets = {
        1: int(round(n_total_target * TARGET_DIST[1])),
        2: int(round(n_total_target * TARGET_DIST[2])),
        3: int(round(n_total_target * TARGET_DIST[3])),  # 3+ bucket
    }

    print(f"Targets: clean={n_clean}, 1-bt={targets[1]}, 2-bt={targets[2]}, 3+-bt={targets[3]}")

    final_examples: List[dict] = list(clean_examples)
    bt_selected: Dict[int, int] = defaultdict(int)

    for n_bt in [1, 2, 3, 4]:
        bucket = min(n_bt, 3)
        still_needed = targets[bucket] - bt_selected[bucket]
        if still_needed <= 0:
            continue

        pool = list(rich_pool.get(n_bt, []))
        rng.shuffle(pool)
        chosen = pool[:still_needed]

        final_examples.extend(chosen)
        bt_selected[bucket] += len(chosen)

        print(
            f"  n_bt={n_bt}: pool={len(pool)}, needed={still_needed}, "
            f"selected={len(chosen)}"
        )

    rng.shuffle(final_examples)

    with open(output_path, "w", encoding="utf-8") as f:
        for ex in final_examples:
            f.write(json.dumps(ex) + "\n")

    # ── Stats ─────────────────────────────────────────────────────────────
    bt_dist: Counter = Counter()
    non_root_count = 0
    for ex in final_examples:
        raw = ex.get("n_backtracks", 0)
        bt_dist[min(raw, 3)] += 1
        if ex.get("has_non_root_backtrack"):
            non_root_count += 1

    n_total = len(final_examples)
    stats = {
        "total_trees": len(tree_files),
        "skipped": skipped,
        "total_examples": n_total,
        "distribution": {
            "0_bt": bt_dist[0],
            "1_bt": bt_dist[1],
            "2_bt": bt_dist[2],
            "3+_bt": bt_dist[3],
        },
        "ratios": {
            "0_bt": round(bt_dist[0] / n_total, 3) if n_total else 0,
            "1_bt": round(bt_dist[1] / n_total, 3) if n_total else 0,
            "2_bt": round(bt_dist[2] / n_total, 3) if n_total else 0,
            "3+_bt": round(bt_dist[3] / n_total, 3) if n_total else 0,
        },
        "non_root_backtracks": non_root_count,
        "output": output_path,
        "flag_file": flag_file,
    }
    print("\n── Final dataset stats ──")
    print(json.dumps(stats, indent=2))

    if flag_file:
        with open(flag_file, "w", encoding="utf-8") as f:
            f.write("Rich backtracking v2 run stats\n")
            f.write(json.dumps(stats, indent=2) + "\n")

    return stats


# ── Quick diagnostic on a single tree (for spot-checking) ────────────────────

def _demo_single_tree(tree_path: str, n_examples: int = 4) -> None:
    """Print sample rich backtracking examples from one tree file."""
    import random as _random
    rng = _random.Random(0)

    with open(tree_path) as f:
        tree = json.load(f)

    nodes = {n["id"]: n for n in tree["nodes"]}
    sol_path, sol_steps = pick_best_solution_path(nodes, tree.get("solutions", []))

    print("=" * 70)
    print(f"Tree: {tree_path}")
    if sol_path:
        print(f"Solution path: {[n['id'] for n in sol_path]}")
        print(f"Solution steps: {sol_steps}")

    candidates = generate_all_bt_candidates_for_tree(tree, rng)
    print(f"Candidates by n_bt: { {k: len(v) for k, v in candidates.items()} }")

    printed = 0
    for n_bt in sorted(candidates.keys()):
        for ex in candidates[n_bt][:2]:
            if printed >= n_examples:
                break
            lvls = ex.get("backtrack_levels", [])
            non_root = ex.get("has_non_root_backtrack", False)
            print(f"\n{'─'*60}")
            print(f"[{n_bt} backtrack(s) | levels={lvls} | non-root={non_root}]")
            print("\nCOMPLETION:")
            print(ex["completion"])
            printed += 1
        if printed >= n_examples:
            break


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # Single-tree demo: python backtracking_augmentation_v2.py <tree.json>
        _demo_single_tree(sys.argv[1])
    elif len(sys.argv) >= 3:
        # Batch mode: python backtracking_augmentation_v2.py <tree_dir> <dead_end_db>
        #             [output_path]
        tree_dir = sys.argv[1]
        db_path = sys.argv[2]
        out = sys.argv[3] if len(sys.argv) > 3 else "training_data_augmented_v2.jsonl"
        generate_augmented_dataset_v2(tree_dir, db_path, out)
    else:
        # Demo on the three sample trees shipped with this script
        for fname in [
            "game24_tree_1_1_2_13_20260527_005649.json",
            "game24_tree_1_1_3_6_20260527_005205.json",
            "game24_tree_1_1_3_8_20260527_002233.json",
        ]:
            if Path(fname).exists():
                _demo_single_tree(fname, n_examples=3)
