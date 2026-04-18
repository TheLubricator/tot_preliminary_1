# Hybrid SER Integration Guide

## Quick Start

### Enable Hybrid SER

```python
from tot_prelim_gemini_COMPLETE import Game24TreeOfThoughts

# Create solver with hybrid SER enabled
solver = Game24TreeOfThoughts(
    temperature=0.7,
    n_evaluate_sample=3,
    n_select_sample=5,      # LLM evaluates top 5 promising moves
    max_steps=6,
    enable_ser=True,         # ← Enable hybrid SER at depth 0
    exhaustive_depth1=False  # Use proposals at depth 1+
)

# Solve a puzzle
numbers = [6, 9, 9, 10]
solutions, root = solver.solve(numbers, verbose=True)

# Display solutions
for solution in solutions:
    print(solution)
```

### What Happens

1. **At Depth 0:**
   - Generates all ~24 possible first moves
   - Filters to ~5 promising moves (fast heuristic check)
   - LLM evaluates only those 5 promising moves
   - Heuristic scores the other ~19 moves (instant, no cost)
   - Selects top 5 from all 24 based on combined scores

2. **At Depths 1+:**
   - Standard LLM proposal mode
   - Dead-End Memory filtering
   - Beam search selection

### Configuration Options

```python
# RECOMMENDED: Balanced approach
solver = Game24TreeOfThoughts(
    temperature=0.7,         # Exploration-exploitation balance
    n_evaluate_sample=3,     # Confidence samples per state
    n_select_sample=5,       # Top 5 moves to keep per level
    max_steps=6,             # Maximum search depth
    enable_ser=True,         # Use hybrid SER at D0
    exhaustive_depth1=False  # Use proposals at D1+
)

# CONSERVATIVE: More exploration
solver = Game24TreeOfThoughts(
    temperature=0.9,         # Higher exploration
    n_evaluate_sample=5,     # More confidence samples
    n_select_sample=10,      # Keep top 10 moves
    enable_ser=True
)

# AGGRESSIVE: Faster but riskier
solver = Game24TreeOfThoughts(
    temperature=0.5,         # Less exploration
    n_evaluate_sample=1,     # Single eval per state
    n_select_sample=3,       # Keep top 3 moves
    enable_ser=True
)

# BASELINE: Pure LLM proposals
solver = Game24TreeOfThoughts(
    enable_ser=False         # Use LLM proposals everywhere
)
```

---

## Understanding the Flow

### The Hybrid SER Process

```
                      Depth 0: [6, 9, 9, 10]
                            ↓
                  ┌─────────────────────┐
                  │ Generate All 24     │
                  │ First Moves         │
                  └─────────────────────┘
                            ↓
      ┌─────────────────────────────────────────┐
      │ Filter Promising Moves                  │
      │ (via passes_basic_heuristics)          │
      │ 24 → ~5 promising candidates           │
      └─────────────────────────────────────────┘
                            ↓
      ╔═══════════════════════════════════════╗
      ║ SPLIT: Promising vs. Other            ║
      ║ ┌────────────────┐  ┌──────────────┐ ║
      ║ │ ~5 Promising   │  │ ~19 Other    │ ║
      ║ └────────────────┘  └──────────────┘ ║
      ║      ↓                    ↓           ║
      ║   LLM Eval         Heuristic Score   ║
      ║  (3 API calls)    (instant, no cost)║
      ║      ↓                    ↓           ║
      ║  value + eval      value (distance)  ║
      ╚═══════════════════════════════════════╝
                            ↓
      ┌─────────────────────────────────────────┐
      │ Combine & Select Top 5 from ALL 24     │
      │ Sort by value (LLM score + heuristic)  │
      │ Take top n_select_sample               │
      └─────────────────────────────────────────┘
                            ↓
              Next Queue (Depth 1)
```

### Decision Logic

**For each move at depth 0:**

1. **Is it promising?**
   - Check: ratio > 1000x? → NO
   - Check: value > 500? → NO
   - Check: 0 < value < 0.01? → NO
   - If all NO: Move is promising!

2. **If promising:** Get LLM evaluation
   - Ask Gemini: "Is this a good move?"
   - Store score (0-1)

3. **If not promising:** Get heuristic score
   - Calculate: distance to 24
   - Formula: 1.0 / (1.0 + distance)
   - Store score (0-1)

4. **Final selection:** Top 5 from all 24
   - Sort all moves by score
   - Take top 5
   - Continue search

---

## Monitoring and Debugging

### Check if Hybrid SER is Running

```python
# In verbose mode, you'll see:
print("  [HYBRID SER] Depth 0: Using hybrid enumeration strategy")
print("    [SER] Generated 24 exhaustive first moves")
print("    [SER] Filtered to 5 promising moves via heuristics")
print("    [LLM] [state] → value: 0.85")
print("    [SER] Selected top 5 moves from all 24")
```

### Statistics

```python
# After solve()
stats = solver.stats
print(f"Total nodes: {stats['total_nodes']}")
print(f"API calls: {stats['api_calls']}")
print(f"Cache hits: {stats['cache_hits']}")
print(f"Dead-end memory:")
print(f"  - Patterns stored: {stats['deadend_memory']['patterns_stored']}")
print(f"  - States skipped: {stats['deadend_memory']['total_skipped']}")
```

### Debugging Hybrid SER

```python
# Enable maximum verbosity
solutions, root = solver.solve(numbers, verbose=True)

# Look for these patterns in output:
# ✓ "[HYBRID SER] Depth 0:" - SER activated
# ✓ "[SER] Generated 24" - All moves created
# ✓ "[SER] Filtered to N" - Filtering happened
# ✓ "[LLM]" entries - LLM evaluations
# ✓ "[SER] Selected" - Final selection made

# If you don't see these, enable_ser might be False
print(f"SER enabled: {solver.enable_ser}")
print(f"Beam width: {solver.n_select_sample}")
```

---

## Common Scenarios

### Scenario 1: Weak Model, Many False Positives

**Problem:** Model says "likely" for almost all moves

**Solution:** Hybrid SER mitigates this by only asking model about promising moves

```python
# Weak model case
solver = Game24TreeOfThoughts(
    enable_ser=True,          # ← Essential for weak models
    n_select_sample=5,
    exhaustive_depth1=False
)

# Why it helps:
# - Instead of asking: "Which of 24 are good?" (answer: "all are likely")
# - Ask: "Which of 5 promising ones are best?" (answer: more trustworthy)
```

### Scenario 2: Need Exhaustive Coverage

**Problem:** Missing solutions because LLM bias cuts off search space

**Solution:** Hybrid SER guarantees all 24 moves are scored

```python
# Guaranteed coverage
solver = Game24TreeOfThoughts(
    enable_ser=True,          # ← Guarantees all 24 moves checked
    exhaustive_depth1=False   # ← But efficient at D1+
)

# Key difference:
# - LLM proposals: Only proposes top 5 (might miss solution paths)
# - Hybrid SER: Scores all 24 (no missed paths)
```

### Scenario 3: Cost-Conscious

**Problem:** Want to reduce API calls

**Solution:** Hybrid SER has same cost as LLM proposals!

```python
# Same cost as baseline
solver_old = Game24TreeOfThoughts(enable_ser=False)
solver_new = Game24TreeOfThoughts(enable_ser=True)

# API calls are IDENTICAL:
# Both make ~3 evaluations at depth 0
# Old: 3 random proposals
# New: 3 best of 24 enumerated moves
```

---

## Advanced Usage

### Custom Heuristic Filtering

If you want different filter thresholds:

```python
# Current defaults (in passes_basic_heuristics):
RATIO_THRESHOLD = 1000    # max_val / min_val
HUGE_NUMBER = 500         # max_val
TINY_FRACTION = 0.01      # 0 < value < this

# To modify, edit passes_basic_heuristics() method:
def passes_basic_heuristics(self, move: Dict) -> bool:
    new_state = move['new_state']
    if len(new_state) != 3:
        return True
    
    max_val = max(abs(x) for x in new_state)
    non_zero = [abs(x) for x in new_state if abs(x) > 1e-9]
    min_val = min(non_zero) if non_zero else max_val
    
    # CUSTOMIZE HERE:
    if max_val / min_val > 500:      # Changed from 1000
        return False
    if max_val > 200:                # Changed from 500
        return False
    if any(0 < abs(x) < 0.05 for x in new_state):  # Changed from 0.01
        return False
    
    return True
```

### Custom Heuristic Scoring

If you want different scoring:

```python
# Current formula (in heuristic_score_move):
sum_dist = abs(sum(new_state) - 24)
prod_dist = abs(prod(new_state) - 24)
sum_score = 1.0 / (1.0 + sum_dist)
prod_score = 1.0 / (1.0 + prod_dist)
score = max(sum_score, prod_score)

# Alternative: exponential decay
def heuristic_score_move_alt(self, move: Dict) -> float:
    new_state = move['new_state']
    if len(new_state) != 3:
        return 1.0
    
    # Exponential scoring
    sum_dist = abs(sum(new_state) - 24)
    prod_dist = abs(prod(new_state) - 24)
    
    import math
    sum_score = math.exp(-sum_dist / 10)  # Decay with distance
    prod_score = math.exp(-prod_dist / 10)
    
    return max(sum_score, prod_score)
```

---

## Performance Tips

### Optimize for Speed

```python
solver = Game24TreeOfThoughts(
    n_select_sample=3,       # Fewer moves to keep
    n_evaluate_sample=1,     # Single eval per state
    enable_ser=True,         # Fast heuristic filtering
    exhaustive_depth1=False
)

# This minimizes API time (3 calls at D0) while keeping SER benefits
```

### Optimize for Accuracy

```python
solver = Game24TreeOfThoughts(
    n_select_sample=10,      # Keep more moves
    n_evaluate_sample=5,     # More confidence samples
    enable_ser=True,         # Still benefits from heuristics
    exhaustive_depth1=False
)

# This costs more but improves solution rate
```

### Optimize for Weak Models

```python
solver = Game24TreeOfThoughts(
    temperature=0.5,         # Less chaotic responses
    n_select_sample=5,       # Evaluate top 5
    enable_ser=True,         # Mitigate overconfidence
    exhaustive_depth1=False
)

# This targets the weak model's specific issues
```

---

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| No "[HYBRID SER]" in output | enable_ser=False | Set `enable_ser=True` |
| Too many states filtered | Thresholds too strict | Adjust passes_basic_heuristics() |
| Too few promising moves | Low threshold hits | Increase threshold values |
| Same cost as baseline | Expected! | That's the design |
| Solution not found | Model bias at D1+ | Not caused by SER |
| Slow performance | API timeout | Increase api_delay |

---

## Next Steps

1. **Test with your puzzles**
   ```python
   your_puzzles = [[1,2,3,4], [6,7,8,9], ...]
   for numbers in your_puzzles:
       solutions, root = solver.solve(numbers)
       print(f"{numbers}: {len(solutions)} solutions found")
   ```

2. **Compare with baseline**
   ```python
   solver_baseline = Game24TreeOfThoughts(enable_ser=False)
   solver_hybrid = Game24TreeOfThoughts(enable_ser=True)
   
   # Run same puzzles, compare success rates
   ```

3. **Monitor performance**
   ```python
   import time
   start = time.time()
   solutions, root = solver.solve(numbers)
   elapsed = time.time() - start
   print(f"Time: {elapsed:.1f}s, Solutions: {len(solutions)}")
   ```

4. **Consider future enhancements**
   - Idea #4: Inconsistency detection
   - Adaptive filtering: Learn thresholds from data
   - Distillation: Use reasoning for student training

---

## Summary

**Hybrid SER provides:**
- ✅ Exhaustive coverage (all 24 moves scored)
- ✅ LLM reasoning (on promising moves)
- ✅ Weak model mitigation (limited bad judgments)
- ✅ Zero cost penalty (same API budget)
- ✅ Easy enablement (single flag)

**Key insight:** Always better than pure LLM proposals, no worse than pure exhaustive evaluation.

Ready to integrate into your Game24 pipeline!
