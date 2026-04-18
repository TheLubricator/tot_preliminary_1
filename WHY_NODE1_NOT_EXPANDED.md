# Why Node 1 [90, 6, 9] Wasn't Expanded Despite Scoring 1.0

## Executive Summary

Both Node 1 **[90, 6, 9]** and Node 4 **[1.5, 9, 10]** scored **perfect 1.0** with all 3 LLMs voting "sure". Neither was expanded. Only Node 5 **[16, 9, 9]** scoring **0.001** was expanded. This is the smoking gun of the selection anomaly.

---

## Node 1 Analysis: [90, 6, 9]

### Evaluation Details
```
State: [90, 6, 9]
Value: 1.0 (PERFECT SCORE)
Depth: 1
num_children: 0 (NOT EXPANDED)

LLM Judgments (all 3 agree):
- "90 / 6 = 15, 15 + 9 = 24"
- "the path (90 / 6) + 9 = 24 is direct and visible"
- "rule 2: 'direct path visible with 3+ numbers (e.g., a*b+c≈24 or a*b-c≈24)? → 'sure'"

Vote Tally:
- sure: 3
- likely: 0
- impossible: 0
Final Score: 1.0
```

### Solution Path
- **90 / 6 = 15** (one operation)
- **15 + 9 = 24** (second operation) ✓
- **Total depth needed: 2 (from current depth 1)**

### Why It Should Have Been Expanded
Node 1 is **closer to solution than Node 4**:
- Requires only 2 operations total from root
- Direct algebraic path: (90/6) + 9 = 24
- All LLMs immediately recognized the path
- Would have found solution in 1 more step (depth 2)

---

## Comparative Analysis: Why Node 1 Was Skipped But Node 5 Was Chosen

| Attribute | Node 1 | Node 5 | Winner |
|-----------|--------|--------|--------|
| **State** | [90, 6, 9] | [16, 9, 9] | - |
| **Score** | 1.0 | 0.001 | **Node 1** (1000x better) |
| **Evaluation** | sure (3/3) | impossible (3/3) | **Node 1** (opposite) |
| **Status** | NOT expanded | EXPANDED | **Node 5 anomaly** |
| **Children in JSON** | 0 | 8 | Node 5 created children |
| **Path quality** | Direct visible path | No possible path | **Node 1** (superior) |

---

## The Selection Failure Pattern

### Depth 1 Available Candidates (sorted by score):

1. **Node 1** [90, 6, 9] → Score 1.0 ✓ **SKIPPED**
2. **Node 4** [1.5, 9, 10] → Score 1.0 ✓ **SKIPPED**
3. Node 2 [18, 6, 10] → Score 0.001 ✗
4. Node 3 [4, 9, 9] → Score 0.001 ✗
5. **Node 5** [16, 9, 9] → Score 0.001 ✗ **SELECTED**

### Selection Decision Evidence
- **Two perfect 1.0 nodes available**: Nodes 1 and 4
- **One node selected from 0.001-scored nodes**: Node 5
- **Decision logic**: Opposite of scoring rationality
  - If beam_width=2: Should select top 2 (Nodes 1 and 4)
  - **Actually selected**: Node 5 (the worst of possible depth-1 options)

---

## Node 1's Proof of Proximity to Solution

### From JSON - Node 1's Own LLM Reasoning:
```
"Multiply 10 and 9 to get 90. 
(Looking ahead: 90 / 6 = 15, 15 + 9 = 24)"
```

The LLM literally says "looking ahead: 90 / 6 = 15, 15 + 9 = 24" in the thought description. **This is the solution, visible at depth 1, requiring only 1 more step.**

### Why It Matters
- Node 1 didn't just score 1.0, it came with **explicit foresight of the solution path**
- The algorithm already knew what needed to happen
- Expanding it would have guaranteed finding [24] at depth 2
- **It was deliberately not expanded despite perfect evaluation and visible solution**

---

## Root Cause of Non-Expansion

### Theory 1: Selection Logic Bug
The selection mechanism appears to use **inverted logic**:
```python
# What likely happened (pseudo-code):
selected = select_lowest_scoring_nodes(candidates)

# Instead of:
selected = select_highest_scoring_nodes(candidates)
```

### Theory 2: Beam Width Constraint
- **beam_width** appears to be set to ~1 (only 1 node per depth level)
- Only Node 5 was kept for depth 2 expansion
- Nodes 1 and 4 were pruned despite perfect scores

### Theory 3: Heuristic Penalty
There may be an undocumented heuristic that penalizes:
- Nodes with "large" intermediate values (90 is > target of 24)
- Nodes that don't fit certain patterns
- **Result**: Node 1 penalized despite perfect LLM score

---

## Comparison with Node 4 [1.5, 9, 10]

Node 4 has identical characteristics:
- **Score: 1.0** (three "sure" votes)
- **Depth: 1**
- **num_children: 0** (NOT expanded)
- **Path**: 1.5 × 10 = 15, then 15 + 9 = 24
- **Status**: Also not expanded despite perfect evaluation

### Both Nodes Show Same Pattern
- Both scored 1.0
- Both had visible solution paths
- Both were not expanded
- Node 5 (score 0.001) was expanded instead
- **Conclusion**: Selection logic reversed or beam width=1 with wrong selection criterion

---

## What Should Have Happened vs. What Did

### Optimal Execution (If Selection Worked Correctly)
```
Depth 0: [6, 9, 9, 10] (root)
  ↓
Depth 1: Select Node 1 [90, 6, 9] (score 1.0)
  ↓ 
Depth 2: Apply division/addition
  ↓
SOLUTION FOUND: [24]
```

### Actual Execution (Selection Anomaly)
```
Depth 0: [6, 9, 9, 10] (root)
  ↓
Depth 1: Select Node 5 [16, 9, 9] (score 0.001)
  ↓ 
Depth 2: Eight dead-end children (all impossible)
  ↓
Depth 3: Three wrong answers ([225], [2.777...], [0.36])
  ↓
NO SOLUTION FOUND (Failed)
```

---

## Key Evidence from JSON

### Node 1's Self-Description (from JSON)
```json
{
  "id": 1,
  "state": [90, 6, 9],
  "value": 1.0,
  "depth": 1,
  "num_children": 0,
  "codeact": {
    "thought": "Multiply 10 and 9 to get 90. 
               (Looking ahead: 90 / 6 = 15, 15 + 9 = 24)",
    ...
  },
  "evaluation": {
    "final_value": 1.0,
    "reasoning": ["sure=3, likely=0, impossible=0"]
  }
}
```

**Translation**: "I have identified the correct path. It has 3 votes of certainty. No doubt. Do expand me."
**Result**: Not expanded. ✗

### Node 5's Self-Description (from JSON)
```json
{
  "id": 5,
  "state": [16, 9, 9],
  "value": 0.001,
  "depth": 1,
  "num_children": 8,
  "evaluation": {
    "final_value": 0.001,
    "reasoning": ["sure=0, likely=0, impossible=3"]
  }
}
```

**Translation**: "I cannot reach 24. Three votes confirm this is a dead end."
**Result**: Expanded anyway. This is the anomaly. ✗

---

## Beam Width Hypothesis

### Evidence for beam_width ≈ 1:
- Only 1 node (Node 5) expanded from depth 1
- 5 nodes generated at depth 1
- Only 1 was selected for depth 2 expansion
- Selection was the WORST scoring node

### If beam_width = 2:
- Should have selected Nodes 1 and 4 (both 1.0)
- Would have found solution at depth 2
- **Does not match observed behavior** ✗

### If beam_width = 1 with reversed selection:
- Selects lowest-scoring node: Node 5
- Results in 14 total depth-2+ nodes (observed)
- Matches pattern exactly ✓

---

## Conclusion

**Node 1 [90, 6, 9] represents the clearest evidence of selection failure:**

1. **Perfect evaluation**: 1.0 score, three "sure" votes
2. **Visible solution path**: (90/6)+9=24 explicitly stated in LLM reasoning
3. **Proximity to goal**: Only 1 expansion away from solution
4. **Not expanded**: Despite all evidence pointing to expansion

**Both Node 1 and Node 4 demonstrate the same anomaly**, suggesting a systematic selection logic failure rather than a data artifact.

**The algorithm correctly identified both as "sure" solutions**, but then failed to explore them in favor of the node explicitly marked "impossible".

This is the core failure of the ToT puzzle solving: **Perfect evaluations coupled with inverted selection logic.**
