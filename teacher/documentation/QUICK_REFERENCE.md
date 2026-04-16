# 🎯 Hybrid Dead-End Memory - Quick Reference

## What Is It?

**Two-level learning system** for Game of 24 solver:
1. **Backend filtering** - Always detects matching patterns
2. **Smart prompting** - LLM learns from THIS puzzle's actual failures

## How to Use

### Default (Hybrid Enabled)
```python
solver = Game24TreeOfThoughts()
solutions, root = solver.solve([1, 4, 8, 8])
```

### Disable Hybrid
```python
solver = Game24TreeOfThoughts(enable_deadend_memory=False)
```

## What You'll See

**Depth 0-1:** Learning phase (no [HYBRID] messages yet)
```
Node 0: Generating proposals...
→ Generated 5 proposals
```

**Depth 2+:** Hybrid active!
```
Node 1: Generating proposals...
[HYBRID] Added 2 learned patterns to prompt  ← LLM learns here
[DEBUG] Calling gemini_generate...
→ Generated 4 proposals

⚠️ Skipping [64, 1, 4] - matches dead-end pattern  ← Backend filters
⚠️ Skipping [65, 4] - matches dead-end pattern
```

## Expected Results

| Metric | Improvement |
|--------|------------|
| API calls | **40-60% reduction** |
| Runtime | **40-60% faster** |
| Daily capacity | **+50%** more puzzles |

## Key Features

✅ Automatic (no config needed)  
✅ Backward compatible  
✅ No extra API costs  
✅ Learns puzzle-specific patterns  
✅ Two-level filtering & prompting  

## Documentation

- **`DEADEND_MEMORY_IMPLEMENTATION.md`** - Full technical details
- **`DEADEND_MEMORY_QUICK_START.md`** - Usage examples
- **`TECHNICAL_CHANGES.md`** - Code modifications
- **`HYBRID_APPROACH_SUMMARY.md`** - Architecture & design
- **`IMPLEMENTATION_SUMMARY.md`** - Complete overview

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No [HYBRID] messages | Waiting for depth 2+ |
| Few skipped states | Reduce `similarity_threshold` |
| Too many skipped states | Increase `similarity_threshold` |
| Want to test without | Set `enable_deadend_memory=False` |

---

**Status: ✅ Ready to use!** Run your solver and watch for hybrid messages starting at depth 2.
