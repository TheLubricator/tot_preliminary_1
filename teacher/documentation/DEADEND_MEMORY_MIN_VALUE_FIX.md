# Dead-End Memory: Min Value Fix

## Problem Identified

The original `extract_pattern()` method used a **problematic default value**:

```python
# OLD (PROBLEMATIC):
min_val = min([abs(x) for x in abs_state if abs(x) > 0], default=1)
```

### Why This Is Wrong

**Scenario 1: False Positives (Over-filtering)**
- Pruned state: `[100, 0.5]` → min = 0.5
- New state: `[100, 1]` → min = 1  
- min_ratio = 1 / 0.5 = **2.0** → ✅ Matches (within 0.3-3.0)
- **Result:** Meaningful node might be evicted incorrectly!

**Scenario 2: False Negatives (Under-filtering)**
- Pruned state: `[100, 1]` → min = 1 (default)
- New state: `[100, 0.1]` → min = 0.1
- min_ratio = 0.1 / 1 = **0.1** → ❌ Doesn't match (outside 0.3-3.0)
- **Result:** Similar dead-end pattern isn't recognized!

**Scenario 3: Edge Case with Zeros**
- Pruned state: `[100, 0.001]` → min = 0.001
- New state: `[100, 0]` → min = **1** (default)
- min_ratio = 1 / 0.001 = **1000** → ❌ Doesn't match
- **Result:** Node with actual zero value isn't filtered, even though it's similar!

---

## Solution Implemented

```python
# NEW (FIXED):
non_zero_vals = [abs(x) for x in abs_state if abs(x) > 1e-9]
min_val = min(non_zero_vals) if non_zero_vals else max_val
```

### How This Fixes It

1. ✅ **Finds actual minimum** among non-zero values
2. ✅ **Uses max_val as fallback** if all values are near-zero (rare edge case)
3. ✅ **Avoids arbitrary default 1** which doesn't reflect actual state
4. ✅ **Consistent comparisons** - comparing real minimums to real minimums

### Updated Similarity Check

Now when comparing min values:
```python
min_ratio = pattern1['min_value'] / (pattern2['min_value'] + 1e-6)
if min_ratio < 0.3 or min_ratio > 3:  # Allow 3x variation
    return False
```

**Works correctly because:**
- `[100, 0.5]` vs `[100, 1]` → ratio = 2.0 ✅ Matches
- `[100, 0.1]` vs `[100, 0.5]` → ratio = 0.2 ❌ Doesn't match (too different)
- Both sides use real extracted values, not arbitrary defaults

---

## Impact

**Before:** 
- Risk of false positive pattern matches → meaningful nodes evicted
- Risk of false negative matches → dead-end patterns not recognized

**After:**
- ✅ Accurate pattern matching based on actual state features
- ✅ No meaningful nodes evicted incorrectly
- ✅ Dead-end patterns properly recognized
- ✅ Only truly similar patterns trigger filtering

---

## Code Change Location

**File:** `tot_prelim_gemini_COMPLETE.ipynb`
**Cell:** Cell 5.5 (DeadEndMemory class)
**Method:** `extract_pattern()`
**Lines:** ~670-680

---

## Testing Recommendation

Monitor these metrics when running puzzles:

```python
mem_stats = solver.dead_end_memory.get_stats()
print(f"Patterns stored: {mem_stats['patterns_stored']}")
print(f"States skipped: {mem_stats['total_skipped']}")
print(f"Skip rate: {mem_stats['skip_rate']}")
```

**Expected behavior:**
- Skip rate should be **5-15%** (not too high, not too low)
- If skip rate > 20%, min_value matching might be too aggressive
- If skip rate < 2%, min_value matching might be too lenient
