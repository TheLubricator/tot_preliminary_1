# LLM Judgment Improvements: Conciseness & Scoring Fixes

## Changes Made

### 1. **Simplified Evaluation Prompt** (Addressing Verbosity)

**Old Prompt:** ~45 lines with extensive rules explanation
**New Prompt:** ~20 lines with concise rules + brief examples

**Key Changes:**
- Removed verbose rule descriptions ("IMPORTANT RULES (follow strictly):", numbered lists, lengthy explanations)
- Added instruction: "Brief check:" to signal the model should be concise
- Added explicit output format: "Brief check:" on a new line followed by model response
- Kept the 3 decision rules (sure/likely/impossible) but stated them simply
- Reduced examples from 4 to 3, with more direct reasoning

**Effect:**
- Model will spend less tokens on exploration and more on decision
- Reduced hallucination of unnecessary arithmetic checks
- Still maintains thorough reasoning for the decision itself

---

### 2. **Fixed Voting Score Mapping** (Addressing Confusion)

**Problem:** 
- Old: `{'impossible': 0.001, 'likely': 1, 'sure': 20}`
- This mapped "sure" and "likely" almost identically (1 vs 20 linear scale)
- Actually worse: the old code used **sum of counts** which turned this into a big number:
  - 3x "likely" votes → score of 3
  - 3x "sure" votes → score of 60
  - This distorted the actual confidence difference

**New Scoring:**
```python
{'impossible': 0.001, 'likely': 0.6, 'sure': 1.0}
```

**How It Works:**
- All three responses averaged (not summed)
- `(sure + sure + sure) / 3 = 1.0` ✓ 
- `(likely + likely + likely) / 3 = 0.6` ✓
- `(impossible + impossible + impossible) / 3 = 0.001` ✓
- Mixed (e.g., 2x sure + 1x likely) = `(1.0 + 1.0 + 0.6) / 3 = 0.867` (reasonable)

**Key Insight:**
This is the **consensus voting model**—averaging is better than summing because:
1. Removes arbitrary scaling issues (20x multiplier)
2. Properly differentiates confidence levels
3. Penalizes disagreement (2 sure + 1 likely = 0.867, not 1.0)
4. Normalizes to [0, 1] range for consistency with beam search

---

## Why Verbosity Matters

Looking at your JSON data, the model's evaluations for `[4, 9, 9]` and `[3, 9, 10]` were **massive blocks of text**:

```
"the numbers are [4, 9, 9].
possible results: 22, 27, 45, 117, 77, 1.8, 72, 0, 4, 5, -3.
none of these are 24.
...
conclusion: impossible.
impossible"
```

**This is wasteful because:**

1. **Token Cost:** Each evaluation response is ~500-800 tokens
   - At depth 1 with 3 evaluations per state: 1500-2400 tokens per state
   - If you evaluate 100 states across a search: 150K-240K tokens just for verbose justification
   - The actual decision ("impossible") is 1 token

2. **Latency:** 1500 tokens takes ~3-5 seconds per state
   - With prompt simplification, likely 500-800 tokens → 1-2 seconds
   - 2-3x speedup per evaluation

3. **Information Density:** After reading 500 tokens, the decision is still just "impossible"
   - The thorough exploration is useful for **training a student model** (showing reasoning)
   - But for **tree search**, you don't need to demonstrate every arithmetic check

4. **Model Tendency:** Given blank canvas + thorough examples, Gemma naturally explores exhaustively
   - Brevity instruction ("Brief check:") nudges it to be concise
   - Doesn't eliminate reasoning, just removes redundant checks

---

## When Verbose is Good vs Bad

### ✅ Verbose Reasoning is GOOD for:
- **Distillation:** Including in final dataset to teach student model "how to think"
- **Post-hoc Analysis:** Understanding why solver made a decision
- **Error Debugging:** Seeing what patterns led to wrong conclusion
- **Idea #1 Implementation:** Thought-based state insights need this depth

### ❌ Verbose is BAD for:
- **Tree Search:** Online evaluation during puzzle solving (costs tokens + time)
- **Weak Model Evaluation:** More text = more chance for contradiction/hallucination
- **Beam Width Scaling:** With n_select_sample=10, you'd evaluate 30+ states per level → massive cost

---

## Recommended Usage

### Option A: Current (Concise, Production)
```python
# Use simplified prompt, brief evaluations
solver = Game24TreeOfThoughts(
    temperature=0.0,  # Deterministic
    n_evaluate_sample=3,
    n_select_sample=5
)
```
**Best for:** Actual puzzle solving, real API costs

### Option B: Verbose Mode for Distillation (Future)
```python
# Save a "verbose config" for post-solution analysis
VALUE_PROMPT_VERBOSE = """...[original 45-line prompt]..."""
# Use after solution found to get detailed reasoning for dataset
```
**Best for:** Building student training dataset

### Option C: Hybrid (Not Implemented Yet)
```python
# During search: use concise prompt (fast)
# On solution path: re-evaluate with verbose prompt (thorough)
# Cost: Amortized because only successful paths get re-eval
```
**Best for:** Production + explainability

---

## Scoring Impact Examples

### Before Fix
State: `[1.5, 9, 10]` (solvable)
- Model votes: [likely, likely, likely]
- Score: `1 * 3 + 0.001 * 0 + 20 * 0 = 3` ❌ Not normalized

State: `[4, 9, 9]` (unsolvable)
- Model votes: [impossible, impossible, impossible]
- Score: `1 * 0 + 0.001 * 3 + 20 * 0 = 0.003` ✓ Low (but inconsistent scale)

### After Fix
State: `[1.5, 9, 10]`
- Model votes: [likely, likely, likely]
- Score: `(0.6 + 0.6 + 0.6) / 3 = 0.6` ✓ Normalized, consistent

State: `[4, 9, 9]`
- Model votes: [impossible, impossible, impossible]
- Score: `(0.001 + 0.001 + 0.001) / 3 = 0.001` ✓ Low, normalized, consistent

**Mixed Case (Novel):**
State: `[7, 8, 9]` (pruned after evaluation)
- Model votes: [sure, likely, likely]
- Score: `(1.0 + 0.6 + 0.6) / 3 = 0.733` ✓ Shows disagreement penalty

---

## Testing Recommendation

Before running full puzzles, test the prompt change:

```python
# Quick test
solver = Game24TreeOfThoughts(enable_ser=False)
test_states = [
    [1.5, 9, 10],  # Should be ~0.6 (likely)
    [4, 9, 9],     # Should be ~0.001 (impossible)
    [15, 9],       # Should be ~1.0 (sure)
]

for state in test_states:
    value, record = solver.evaluate_state(state)
    print(f"{state}: {value:.3f}")
```

Expected output:
```
[1.5, 9, 10]: 0.600
[4, 9, 9]: 0.001
[15, 9]: 1.000
```

---

## Summary of Fixes

| Issue | Before | After | Benefit |
|-------|--------|-------|---------|
| **Prompt Verbosity** | 45 lines | 20 lines | 2-3x faster evaluations |
| **Score Mapping** | 0-20 scale, confusing | 0-1 normalized | Consistent with beam search |
| **Aggregation** | Sum of weighted counts | Average of values | Proper consensus voting |
| **Decision Clarity** | Mixed with exploration | Clear "Brief check:" signal | Model knows to be concise |
| **Differentiation** | "sure" vs "likely" both ~1 | 1.0 vs 0.6 distinction | Better ranking |

---

**Ready for:** Testing on actual Game24 puzzles to measure token savings and speed improvements!
