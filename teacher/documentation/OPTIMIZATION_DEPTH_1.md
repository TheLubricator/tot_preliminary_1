# 🚀 Optimization: Depth Threshold Adjustment

**Date:** April 16, 2026  
**Change:** Hybrid approach now activates at **Depth ≥ 1** (was Depth ≥ 2)  
**Impact:** Better API call savings + earlier pattern learning

---

## 📊 Why This Is Better

### Analysis of Search Depths

```
Depth 0 (4 numbers): Initial state
         ↓ Generate proposals
Depth 1 (3 numbers): ← BIGGEST EXPLOSION OF PROPOSALS!
         ↓ Generate proposals
Depth 2 (2 numbers): Can be evaluated with hard-coded logic
         ↓ Final operation
Depth 3 (1 number):  Either solution or automatic failure
```

### Where Dead-End Memory Helps Most

**Depth 0→1 transition:**
- Start with 1 proposal set
- Generate many different 3-number states
- Many will be dead-ends
- **Perfect place to filter with learned patterns** ✅

**Depth 1→2 transition:**
- Already have some patterns from depth 0
- Fewer states to filter (depth 1 pruning already happened)
- **Still helpful, but less critical**

**Depth 2→3 transition:**
- Only 1 number remains
- Hard-coded 2-number check is already optimal
- **Dead-End Memory less relevant here**

---

## 💾 Code Change

```python
# OLD: Start at depth 2
if self.dead_end_memory is not None and current_depth >= 2:
    pattern_summary = ...

# NEW: Start at depth 1 (catch the explosion earlier)
if self.dead_end_memory is not None and current_depth >= 1:
    pattern_summary = ...
```

---

## 📈 Expected Improvement

**With Depth ≥ 1:**
- Hybrid prompting starts at depth 1
- Catches the 3→2 number explosion with learned patterns
- LLM generates better proposals earlier
- **Better API call savings throughout search**

**With Depth ≥ 2:**
- Hybrid prompting starts at depth 2 (too late)
- Most of the explosion already happened
- Fewer proposals to filter
- Less effective

---

## ✅ Summary

**Old approach:** Depth ≥ 2 (conservative, waits for more patterns)
**New approach:** Depth ≥ 1 (aggressive, catches explosion earlier)

**Result:** More efficient search with better pattern learning at the right time! 🎯

---

**Status:** ✅ Updated in notebook and documentation
