"""
debug_output_qwen05.py  ── Raw output inspection for Qwen2.5-0.5B-Instruct
===========================================================================
Usage:
    python debug_output_qwen05.py --model ./qwen05_finetuned --csv 24.csv

Optional flags:
    --n 5                   number of puzzles to test (default 5)
    --max_new_tokens 300    generation budget (default 300)
    --sample                stochastic decoding (default: greedy)
    --rank_start 901        start rank for test puzzles (default 901)
    --rank_end   1000       end rank for test puzzles (default 1000)
    --verify                run arithmetic verifier on each output
    --log FILE              save all printed output to FILE (default debug_output.txt)

Side-by-side comparison mode (requires SmolLM output for same puzzles):
    python debug_output_qwen05.py --model ./qwen05_finetuned --csv 24.csv --compare
    (runs greedy on same 5 puzzles and prints Qwen vs SmolLM side-by-side)
"""

import argparse
import csv
import re
import sys
import torch
from fractions import Fraction
from transformers import AutoModelForCausalLM, AutoTokenizer

# ── Tee class to duplicate stdout to a file ───────────────────────────────────
class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--model",          default="./qwen05_finetuned")
parser.add_argument("--csv",            default="24.csv")
parser.add_argument("--n",              type=int, default=5)
parser.add_argument("--max_new_tokens", type=int, default=300)
parser.add_argument("--rank_start",     type=int, default=901)
parser.add_argument("--rank_end",       type=int, default=1000)
parser.add_argument("--sample",         action="store_true",
                    help="Stochastic decoding (temperature=0.3, top_p=0.95)")
parser.add_argument("--verify",         action="store_true",
                    help="Run step-level arithmetic verifier on each output")
parser.add_argument("--log",            default="debug_output.txt",
                    help="Save all printed output to this file (default: debug_output.txt)")
args = parser.parse_args()

# ── Redirect stdout to Tee (console + log file) ───────────────────────────────
original_stdout = sys.stdout
log_file = open(args.log, "w", encoding="utf-8")
sys.stdout = Tee(original_stdout, log_file)

try:
    # ── Load puzzles ──────────────────────────────────────────────────────────────

    with open(args.csv) as f:
        rows = list(csv.DictReader(f))

    test_puzzles = [
        r["Puzzles"].strip()
        for r in rows
        if args.rank_start <= int(r["Rank"]) <= args.rank_end
    ]
    test_puzzles = test_puzzles[:args.n]
    print(f"Testing {len(test_puzzles)} puzzles (rank {args.rank_start}–{args.rank_end})\n")

    # ── Prompts — must match training exactly ─────────────────────────────────────

    SYSTEM_PROMPT = (
        "Use numbers and basic arithmetic operations (+, -, *, /) to obtain 24. "
        "Each step, you are only allowed to choose two of the remaining numbers to obtain a new number.\n"
        "Step 1: Start by considering possible operations for each pair of numbers.\n"
        "Step 2: Try a path (a pair of two numbers), see if the remaining numbers can possibly reach the goal 24. If not, backtrack and attempt another.\n"
        "Step 3: Branch out to try different orders of operations and combinations, evaluating each outcome.\n"
        "Step 4: If one path doesn't lead to a solution, backtrack and try alternative operations.\n\n"
    )

    IN_CONTEXT_HEADER = (
        "Here are some solved examples:\n\n"
        "Numbers: 4 4 6 8. Target: 24.\n"
        "Use each number exactly once with +, -, *, / to reach 24.\n"
        "Steps:\n"
        "4 + 8 = 12 (left: 4 6 12)\n"
        "6 - 4 = 2 (left: 2 12)\n"
        "2 * 12 = 24 (left: 24)\n"
        "Answer: (6 - 4) * (4 + 8) = 24\n\n"
        "Numbers: 1 4 8 8. Target: 24.\n"
        "Use each number exactly once with +, -, *, / to reach 24.\n"
        "Steps:\n"
        "8 / 4 = 2 (left: 1 2 8)\n"
        "1 + 2 = 3 (left: 3 8)\n"
        "3 * 8 = 24 (left: 24)\n"
        "Answer: (1 + 8 / 4) * 8 = 24\n\n"
        "Numbers: 5 5 5 9. Target: 24.\n"
        "Use each number exactly once with +, -, *, / to reach 24.\n"
        "Steps:\n"
        "5 + 5 = 10 (left: 5 9 10)\n"
        "10 + 5 = 15 (left: 9 15)\n"
        "15 + 9 = 24 (left: 24)\n"
        "Answer: ((5 + 5) + 5) + 9 = 24\n\n"
        "Numbers: 4 9 10 13. Target: 24.\n"
        "Use each number exactly once with +, -, *, / to reach 24.\n"
        "Steps:\n"
        "4 + 9 = 13 (left: 10 13 13)\n"
        "13 - 10 = 3 (left: 3 13)\n"
        "13 + 3 = 16 (left: 16)\n"
        "Backtrack. (to: 4 9 10 13).\n"
        "13 - 10 = 3 (left: 3 4 9)\n"
        "9 - 3 = 6 (left: 4 6)\n"
        "4 * 6 = 24 (left: 24)\n"
        "Answer: 4 * (9 - (13 - 10)) = 24\n\n"
        "Now solve this puzzle:\n"
    )

    def make_puzzle_block(puzzle):
        return (
            f"Numbers: {puzzle}. Target: 24.\n"
            f"Use each number exactly once with +, -, *, / to reach 24.\n"
            f"Steps:"
        )

    def make_user_content(puzzle):
        return SYSTEM_PROMPT + IN_CONTEXT_HEADER + make_puzzle_block(puzzle)

    # ── Load model ────────────────────────────────────────────────────────────────

    print(f"Loading {args.model} ...")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model     = AutoModelForCausalLM.from_pretrained(
        args.model,
        device_map="auto",
        torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
    )
    model.eval()
    device = next(model.parameters()).device
    print(f"Model on {device}\n")

    # ── Verifier ──────────────────────────────────────────────────────────────────

    def verify_output(puzzle, response_text):
        """
        Step-level verifier. Returns a dict with:
          - step_errors: list of (step_text, error_msg)
          - answer_correct: bool
          - answer_msg: str
          - arithmetic_accuracy: float (fraction of steps with correct arithmetic)
        """
        puzzle_nums = [Fraction(x) for x in puzzle.strip().split()]
        lines = [l.strip() for l in response_text.strip().split('\n') if l.strip()]

        step_errors   = []
        steps_checked = 0
        steps_ok      = 0
        available     = list(puzzle_nums)
        answer_correct = False
        answer_msg     = "No Answer line found"

        for lt in lines:
            # ── Backtrack ────────────────────────────────────────────────────────
            if lt.startswith('Backtrack.'):
                bm = re.search(r'\(to:\s*(.*?)\)', lt)
                if bm:
                    # Extract only valid number tokens — skip operator chars.
                    # Guards against malformed targets like "(to: 12 - 21)"
                    # where the model generated an expression instead of a pool.
                    raw_tokens = bm.group(1).strip().split()
                    parsed = []
                    for tok in raw_tokens:
                        try:
                            parsed.append(Fraction(tok))
                        except (ValueError, ZeroDivisionError):
                            pass  # skip operators like '-', '+', malformed tokens
                    available = parsed if parsed else list(puzzle_nums)
                else:
                    available = list(puzzle_nums)
                continue

            # ── Answer line ───────────────────────────────────────────────────────
            if lt.startswith('Answer:'):
                am = re.match(r'Answer:\s*(.+?)\s*=\s*24', lt)
                if not am:
                    answer_msg = f"Cannot parse: {lt!r}"
                    continue
                expr = am.group(1)
                try:
                    expr_f  = re.sub(r'\b(\d+)\b', r'Fraction(\1)', expr)
                    result  = eval(expr_f, {"Fraction": Fraction, "__builtins__": {}})
                    nums_used = sorted(int(x) for x in re.findall(r'\b\d+\b', expr))
                    expected  = sorted(int(x) for x in puzzle.strip().split())
                    if result == 24 and nums_used == expected:
                        answer_correct = True
                        answer_msg     = "✓ Correct"
                    elif result != 24:
                        answer_msg = f"✗ Evaluates to {float(result):.4g}, not 24"
                    else:
                        answer_msg = f"✗ Wrong numbers used: {nums_used} vs {expected}"
                except Exception as e:
                    answer_msg = f"✗ Eval error: {e}"
                continue

            # ── Step line ─────────────────────────────────────────────────────────
            m = re.match(
                r'(-?[\d.]+)\s*([+\-*/])\s*(-?[\d.]+)\s*=\s*(-?[\d.]+)\s*\(left:\s*(.*?)\)',
                lt
            )
            if not m:
                continue  # not a step line (e.g. truncated output)

            steps_checked += 1
            a_s, op, b_s, c_s, left_s = m.groups()
            try:
                a, b, c = Fraction(a_s), Fraction(b_s), Fraction(c_s)
            except Exception:
                step_errors.append((lt, "Could not parse numbers as fractions"))
                continue

            ops_map = {'+': a+b, '-': a-b, '*': a*b,
                       '/': (a/b if b != 0 else None)}
            expected_c = ops_map.get(op)

            if expected_c is None:
                step_errors.append((lt, "Division by zero"))
            elif expected_c != c:
                step_errors.append((lt,
                    f"Arithmetic: {float(a)} {op} {float(b)} should be "
                    f"{float(expected_c):.4g}, got {float(c):.4g}"))
            else:
                steps_ok += 1
                # Update available
                try:
                    available.remove(a)
                    available.remove(b)
                except ValueError:
                    step_errors.append((lt,
                        f"Used numbers not in pool: {float(a)}, {float(b)} "
                        f"not in {[float(x) for x in available]}"))
                    steps_ok -= 1  # undo
                    available.append(c)
                    continue
                available.append(c)

        accuracy = steps_ok / steps_checked if steps_checked > 0 else 0.0
        return {
            "step_errors":        step_errors,
            "answer_correct":     answer_correct,
            "answer_msg":         answer_msg,
            "arithmetic_accuracy": accuracy,
            "steps_checked":      steps_checked,
        }

    # ── Generate ──────────────────────────────────────────────────────────────────

    results_summary = []

    for puzzle in test_puzzles:
        user_content = make_user_content(puzzle)

        # Build prompt using Qwen's chat template
        messages = [{"role": "user", "content": user_content}]
        prompt_text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
        input_len = inputs["input_ids"].shape[1]

        sample_kwargs = {}
        if args.sample:
            sample_kwargs = {"temperature": 0.3, "top_p": 0.95}

        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=args.sample,
                **sample_kwargs,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

        gen_ids  = out[0][input_len:]
        response = tokenizer.decode(gen_ids, skip_special_tokens=False)
        response_clean = tokenizer.decode(gen_ids, skip_special_tokens=True).strip()

        print(f"\n{'='*60}")
        print(f"PUZZLE: {puzzle}")
        print(f"DECODING: {'sampling (t=0.3)' if args.sample else 'greedy'}")
        print(f"\nRAW OUTPUT (with special tokens):")
        print(repr(response))
        print(f"\nDECODED:")
        print(response_clean)

        # ── Verify ────────────────────────────────────────────────────────────────
        if args.verify:
            v = verify_output(puzzle, response_clean)
            print(f"\nVERIFICATION:")
            print(f"  Arithmetic accuracy: {v['arithmetic_accuracy']:.0%} "
                  f"({v['steps_checked']} steps checked)")
            print(f"  Answer line: {v['answer_msg']}")
            if v['step_errors']:
                print(f"  Step errors ({len(v['step_errors'])}):")
                for step_text, err in v['step_errors']:
                    print(f"    • {step_text!r}")
                    print(f"      → {err}")
            else:
                print(f"  No step errors ✓")

            results_summary.append({
                "puzzle":       puzzle,
                "answer_ok":    v['answer_correct'],
                "arith_acc":    v['arithmetic_accuracy'],
                "step_errors":  len(v['step_errors']),
            })

    # ── Summary table (only with --verify) ───────────────────────────────────────

    if args.verify and results_summary:
        correct = sum(1 for r in results_summary if r['answer_ok'])
        avg_acc = sum(r['arith_acc'] for r in results_summary) / len(results_summary)
        total_step_errs = sum(r['step_errors'] for r in results_summary)

        print(f"\n{'='*60}")
        print(f"SUMMARY ({len(results_summary)} puzzles)")
        print(f"{'='*60}")
        print(f"  Correct answers (= 24):  {correct}/{len(results_summary)}")
        print(f"  Avg arithmetic accuracy: {avg_acc:.0%}")
        print(f"  Total step errors:       {total_step_errs}")
        print(f"\n  Per-puzzle breakdown:")
        for r in results_summary:
            status = "✓" if r['answer_ok'] else "✗"
            print(f"  {status} {r['puzzle']:<15} "
                  f"arith={r['arith_acc']:.0%}  "
                  f"step_errs={r['step_errors']}")

finally:
    # Restore original stdout and close log file
    sys.stdout = original_stdout
    log_file.close()
    print(f"\n[All output also saved to {args.log}]")