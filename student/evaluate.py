"""
evaluate.py  ── test fine-tuned model on puzzles 901-1000
Usage: python evaluate.py --model ./smollm_finetuned_best --csv 24.csv
"""

import argparse, csv, json, re
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="./smollm_finetuned_best")
parser.add_argument("--csv",   default="24.csv")
parser.add_argument("--start", type=int, default=901)
parser.add_argument("--end",   type=int, default=1000)
parser.add_argument("--max_new_tokens", type=int, default=200)
parser.add_argument("--out",   default="eval_results.json")
args = parser.parse_args()

with open(args.csv) as f:
    all_rows = list(csv.DictReader(f))
test_puzzles = [
    r["Puzzles"].strip()
    for r in all_rows
    if args.start <= int(r["Rank"]) <= args.end
]
print(f"Test set: {len(test_puzzles)} puzzles (rank {args.start}–{args.end})")

print(f"Loading {args.model} ...")
tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=False)
model     = AutoModelForCausalLM.from_pretrained(args.model, device_map="auto")
model.eval()
device = next(model.parameters()).device

def make_prompt(puzzle):
    # MUST match training format exactly — note \n after assistant
    user_content = (
        f"Numbers: {puzzle}. Target: 24.\n"
        f"Use each number exactly once with +, -, *, / to reach 24.\n"
        f"Steps:"
    )
    return f"<|im_start|>user\n{user_content}<|im_end|>\n<|im_start|>assistant\n"

def extract_answer_expr(response):
    m = re.search(r"Answer:\s*(.+)", response)
    return m.group(1).strip() if m else None

def verify(puzzle, response):
    numbers = list(map(int, puzzle.split()))
    expr    = extract_answer_expr(response)
    if expr is None:
        return False
    expr = re.sub(r"^Answer:\s*", "", expr).strip()
    allowed = set("0123456789+-*/()= .")
    if not set(expr).issubset(allowed):
        return False
    expr_eval = re.sub(r"\s*=\s*24\s*$", "", expr).strip()
    if not expr_eval:
        return False
    try:
        result = eval(expr_eval, {"__builtins__": {}})
    except Exception:
        return False
    if abs(result - 24) > 1e-6:
        return False
    used = list(map(int, re.findall(r"\d+", expr_eval)))
    return sorted(used) == sorted(numbers)

results   = []
correct   = 0
no_answer = 0

print(f"\nEvaluating {len(test_puzzles)} puzzles...\n")

for i, puzzle in enumerate(test_puzzles, 1):
    prompt = make_prompt(puzzle)
    inputs = tokenizer(prompt, return_tensors="pt").to(device)

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    gen_ids  = output_ids[0][inputs["input_ids"].shape[1]:]
    response = tokenizer.decode(gen_ids, skip_special_tokens=True).strip()

    ok        = verify(puzzle, response)
    expr      = extract_answer_expr(response)
    correct  += int(ok)
    no_answer += int(expr is None)

    status = "✓" if ok else "✗"
    print(f"  [{i:3d}/100] {puzzle:15s} {status}  {expr or '(no answer)'}")

    results.append({
        "rank":    args.start + i - 1,
        "puzzle":  puzzle,
        "response": response,
        "answer":  expr,
        "correct": ok,
    })

success_rate = correct / len(test_puzzles) * 100
print(f"\n{'='*50}")
print(f"  Correct:      {correct} / {len(test_puzzles)}")
print(f"  Success rate: {success_rate:.1f}%")
print(f"  No answer:    {no_answer}")
print(f"  Paper baseline (their fine-tuned SmolLM): 9%")
print(f"{'='*50}")

with open(args.out, "w") as f:
    json.dump({
        "model": args.model,
        "correct": correct,
        "success_rate_pct": round(success_rate, 2),
        "no_answer": no_answer,
        "results": results,
    }, f, indent=2)
print(f"\nResults → {args.out}")
