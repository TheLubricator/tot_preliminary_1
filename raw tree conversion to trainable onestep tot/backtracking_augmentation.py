"""
backtracking_augmentation.py
==============================
Fix 1 (Training-side): Generates augmented backtracking training examples
using verified dead-end branches from the tree JSON.

Updated version:
  - Flags trees that have solutions but failed to produce a clean training example.
  - Writes a separate file with problematic tree filenames.
  - Logs reason for failure where available (from extract_training_example return code).
"""

import json
import re
import random
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# Re-use the core utilities from extract_training_example.py
from extract_training_example import (
    format_step,
    fmt_num,
    trace_solution_path,
    build_final_expression,
    get_relevant_deadend_patterns,
    should_include_deadend_context,
    format_deadend_context,
    extract_training_example,
    _operands_in_state,
    HIT_COUNT_THRESHOLD,
)

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

BACKTRACK_RATIO = 0.20      # Target: 20% of total examples are backtracking
PRUNED_VALUE_THRESHOLD = 0.1  # Depth-1 node is a dead end if value ≤ this
DEAD_END_TAG = "[dead end]"


# ─────────────────────────────────────────────
#  Find candidate depth-1 dead-end nodes
# ─────────────────────────────────────────────

def find_depth1_dead_ends(nodes_list: list) -> list:
    """Return all depth-1 nodes that are verified dead ends."""
    dead_ends = []
    for node in nodes_list:
        if node["depth"] != 1:
            continue
        if node.get("is_solution", False):
            continue
        is_pruned = node.get("is_pruned", False)
        is_low_value = node.get("value", 1.0) <= PRUNED_VALUE_THRESHOLD
        has_code = bool(node.get("codeact", {}).get("code", "").strip())
        if (is_pruned or is_low_value) and has_code:
            dead_ends.append(node)
    return dead_ends


# ─────────────────────────────────────────────
#  Build one backtracking example
# ─────────────────────────────────────────────

def extract_backtracking_example(
    tree: dict,
    dead_end_db: dict,
    dead_end_node: dict,
    solution_steps: list,
    solution_path: list,
    puzzle_str: str,
) -> Optional[dict]:
    """Build a backtracking example (dead-end step + recovery)."""
    dead_end_line = format_step(dead_end_node)
    if dead_end_line is None:
        return None

    final_expr = build_final_expression(solution_path, solution_steps)

    nodes_list = tree["nodes"]
    puzzle_numbers = tree["nodes"][0]["state"]
    explored_states = set()
    for n in nodes_list:
        if n["depth"] > 0 and n["state"] and len(n["state"]) > 1:
            key = str(sorted(int(float(x)) for x in n["state"]))
            explored_states.add(key)

    relevant_patterns = get_relevant_deadend_patterns(
        puzzle_numbers, dead_end_db, explored_states
    )
    include_deadend_ctx = should_include_deadend_context(tree, relevant_patterns)

    prompt_lines = [
        f"Numbers: {puzzle_str}. Target: 24.",
        "Use each number exactly once with +, -, *, / to reach 24.",
    ]
    if include_deadend_ctx:
        prompt_lines.append(format_deadend_context(relevant_patterns))
    prompt_lines.append("Steps:")

    completion_lines = [f"{dead_end_line} {DEAD_END_TAG}"]
    completion_lines.append(f"[restart: {puzzle_str}]")  # explicit state reset — model must not carry over left: from dead end
    completion_lines.extend(solution_steps)
    completion_lines.append(f"Answer: {final_expr}")

    return {
        "puzzle": puzzle_str,
        "prompt": "\n".join(prompt_lines),
        "completion": "\n".join(completion_lines),
        "example_type": "backtracking",
        "has_deadend_ctx": include_deadend_ctx,
        "dead_end_step": dead_end_line,
        "dead_end_node_id": dead_end_node["id"],
        "dead_end_value": dead_end_node.get("value", 0.0),
        "solution_steps": len(solution_steps),
    }


# ─────────────────────────────────────────────
#  Get backtracking candidate for one tree
# ─────────────────────────────────────────────

def get_backtracking_examples_for_tree(tree: dict, dead_end_db: dict) -> list:
    """Return at most one backtracking example per tree."""
    nodes_list = tree["nodes"]
    nodes = {n["id"]: n for n in nodes_list}
    solution_ids = tree.get("solutions", [])

    if not solution_ids:
        return []

    # Find shortest valid solution path
    best_sol = None
    best_steps = None
    best_path = None
    for sol_id in solution_ids:
        path = trace_solution_path(nodes, sol_id)
        steps = []
        valid = True
        for k, n in enumerate(path[1:], 1):
            line = format_step(n)
            if line is None:
                valid = False
                break
            # Type A guard: operands must exist in parent state
            import re as _re
            m_op = _re.match(r'(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)', line)
            if m_op:
                a_val, b_val = float(m_op.group(1)), float(m_op.group(2))
                if not _operands_in_state(a_val, b_val, path[k - 1]["state"]):
                    valid = False
                    break
            steps.append(line)
        if valid and steps:
            if best_sol is None or len(steps) < len(best_steps):
                best_sol, best_steps, best_path = sol_id, steps, path

    if best_sol is None:
        return []

    dead_ends = find_depth1_dead_ends(nodes_list)
    if not dead_ends:
        return []

    dead_ends.sort(key=lambda n: n.get("value", 0.0))
    chosen_dead_end = None
    for de in dead_ends:
        line = format_step(de)
        if line and line != best_steps[0]:
            chosen_dead_end = de
            break

    if chosen_dead_end is None:
        return []

    puzzle_numbers = nodes[0]["state"]
    puzzle_str = " ".join(str(int(float(x))) for x in puzzle_numbers)

    example = extract_backtracking_example(
        tree, dead_end_db, chosen_dead_end,
        best_steps, best_path, puzzle_str
    )
    return [example] if example else []


# ─────────────────────────────────────────────
#  Batch processor with flagging
# ─────────────────────────────────────────────

def generate_augmented_dataset(
    tree_dir: str,
    dead_end_db_path: str,
    output_path: str = "training_data_augmented.jsonl",
    target_backtrack_ratio: float = BACKTRACK_RATIO,
    flag_file: Optional[str] = "flagged_problematic_trees.txt",
    seed: int = 42,
) -> dict:
    """
    Process all tree JSONs and generate mixed dataset with flagging.

    Args:
        tree_dir:               Directory containing game24_tree_*.json files.
        dead_end_db_path:       Path to dead_end_db.json.
        output_path:            Output JSONL file path.
        target_backtrack_ratio: Fraction of dataset to be backtracking.
        flag_file:              If provided, write problematic tree filenames to this file.
        seed:                   Random seed.

    Returns:
        Summary statistics dict.
    """
    random.seed(seed)

    with open(dead_end_db_path, encoding='utf-8') as f:
        dead_end_db = json.load(f)

    tree_files = sorted(Path(tree_dir).glob("game24_tree_*.json"))
    print(f"Found {len(tree_files)} tree files.")

    clean_examples = []
    backtrack_candidates = []
    clean_failed_despite_solution = []   # list of (filename, reason)
    backtrack_failed_despite_solution = []
    skipped_no_solution = 0

    for tree_path in tree_files:
        with open(tree_path, encoding='utf-8') as f:
            tree = json.load(f)

        has_solution = len(tree.get("solutions", [])) > 0

        # Extract clean example
        clean = extract_training_example(tree, dead_end_db)
        if clean is None:
            if has_solution:
                # Flag this tree – it has solution but extraction failed
                clean_failed_despite_solution.append((tree_path.name, "extract_training_example returned None"))
            else:
                skipped_no_solution += 1
            # Still try to get backtracking examples? No, because backtracking requires a correct solution path.
            # But we can still attempt to collect backtracking candidates if a solution exists? 
            # Actually backtracking needs a correct solution path, so if clean extraction failed, 
            # backtracking will also fail because it uses the same path.
            continue

        clean["example_type"] = "clean"
        clean_examples.append(clean)

        # Backtracking candidates
        bt_examples = get_backtracking_examples_for_tree(tree, dead_end_db)
        if not bt_examples and has_solution:
            # Tree has solution but no backtracking example (maybe no depth-1 dead-end or formatting issues)
            backtrack_failed_despite_solution.append(tree_path.name)
        backtrack_candidates.extend(bt_examples)

    n_clean = len(clean_examples)
    if n_clean == 0:
        print("No valid clean examples found.")
        return {}

    # Ratio control
    max_bt = int(round(n_clean * target_backtrack_ratio / (1 - target_backtrack_ratio)))
    if len(backtrack_candidates) > max_bt:
        backtrack_examples = random.sample(backtrack_candidates, max_bt)
        print(f"Sampled {max_bt} backtracking examples from {len(backtrack_candidates)} candidates to hit {target_backtrack_ratio:.0%} target ratio.")
    else:
        backtrack_examples = backtrack_candidates
        actual_ratio = len(backtrack_examples) / (n_clean + len(backtrack_examples)) if backtrack_examples else 0
        print(f"Only {len(backtrack_examples)} backtracking candidates available — actual ratio will be {actual_ratio:.1%}.")

    # Write flag file if requested
    if flag_file:
        with open(flag_file, 'w', encoding='utf-8') as f:
            f.write("Trees with solutions but clean example extraction failed:\n")
            for fname, reason in clean_failed_despite_solution:
                f.write(f"  {fname}  (reason: {reason})\n")
            if not clean_failed_despite_solution:
                f.write("  None\n")
            f.write("\nTrees with solutions but no backtracking candidate (may be OK if no dead-end existed):\n")
            for fname in backtrack_failed_despite_solution:
                f.write(f"  {fname}\n")
            if not backtrack_failed_despite_solution:
                f.write("  None\n")
        print(f"Flagged problematic trees written to {flag_file}")

    # Interleave and shuffle
    all_examples = clean_examples + backtrack_examples
    random.shuffle(all_examples)

    with open(output_path, "w") as f:
        for ex in all_examples:
            f.write(json.dumps(ex) + "\n")

    n_bt = len(backtrack_examples)
    n_total = len(all_examples)
    stats = {
        "total_trees": len(tree_files),
        "skipped_no_solution": skipped_no_solution,
        "clean_examples": n_clean,
        "backtracking_examples": n_bt,
        "total_examples": n_total,
        "backtrack_ratio": round(n_bt / n_total, 3) if n_total else 0,
        "clean_with_deadend_ctx": sum(1 for e in clean_examples if e.get("has_deadend_ctx")),
        "avg_solution_steps": round(sum(e["solution_steps"] for e in clean_examples) / n_clean, 2),
        "clean_failed_despite_solution": len(clean_failed_despite_solution),
        "backtrack_failed_despite_solution": len(backtrack_failed_despite_solution),
        "output": output_path,
        "flag_file": flag_file if flag_file else None,
    }

    print("\n── Dataset stats ──")
    print(json.dumps(stats, indent=2))
    return stats


# ─────────────────────────────────────────────
#  Quick test on a single tree (for debugging)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    TREE_PATH = "game24_tree_1_3_6_11_20260525_175624.json"
    DB_PATH   = "dead_end_db.json"

    with open(TREE_PATH, encoding='utf-8') as f:
        tree = json.load(f)
    with open(DB_PATH, encoding='utf-8') as f:
        db = json.load(f)

    print("=" * 60)
    print("CLEAN EXAMPLE")
    print("=" * 60)
    clean = extract_training_example(tree, db)
    if clean:
        clean["example_type"] = "clean"
        print(f"Puzzle: {clean['puzzle']}")
        print(f"Has dead-end INPUT context: {clean['has_deadend_ctx']}")
        print()
        print("── PROMPT ──")
        print(clean["prompt"])
        print()
        print("── COMPLETION ──")
        print(clean["completion"])
    else:
        print("No valid clean example extracted.")

    print()
    print("=" * 60)
    print("BACKTRACKING EXAMPLES")
    print("=" * 60)
    bt_examples = get_backtracking_examples_for_tree(tree, db)
    if bt_examples:
        for i, bt in enumerate(bt_examples, 1):
            print(f"\n[Backtracking example {i}]")
            print(f"Dead-end node id: {bt['dead_end_node_id']} | value: {bt['dead_end_value']}")
            print(f"Dead-end step:    {bt['dead_end_step']}")
            print()
            print("── PROMPT ──")
            print(bt["prompt"])
            print()
            print("── COMPLETION ──")
            print(bt["completion"])
    else:
        print("No backtracking examples could be generated for this tree.")