# 🧠 TiM Idea #4: Inconsistency Detection
## Comprehensive Expansion & Analysis

---

## 📌 Core Problem Statement

### The Issue in Tree of Thoughts
When a Tree of Thoughts solver evaluates the same game state **multiple times** (at different search depths or in different contexts), the LLM might assign **drastically different scores** to the same state.

**Example from Game of 24:**
```
State [9, 6, 10] (can make 24 → 6*(10-9) = 6)

Evaluation 1 (Depth 1, fresh context):
  Score: 0.8 (likely) ✓
  Reasoning: "6 and 10 are close to 24, could combine them"

Evaluation 2 (Depth 3, different context):
  Score: 0.1 (impossible) ✗
  Reasoning: "9, 6, 10 - no obvious path exists"

Variance: 0.7 (70% difference!)
⚠️  PROBLEM: LLM gave contradictory assessments of the SAME state
```

### Why This Happens

1. **Context Window Effect**
   - At Depth 1: Only 4 numbers, easy to reason about
   - At Depth 3: Different numbers in context, LLM "forgets" about [9,6,10]
   - LLM re-reasons from scratch, gets different answer

2. **Temperature/Sampling Variance**
   - If using temperature > 0, different samples = different responses
   - Same question → different reasoning paths → different conclusions

3. **Prompt Formulation**
   - How state is presented in prompt matters
   - Different nearby states affect reasoning (anchoring bias)

4. **Tokenization & Representation**
   - How numbers are formatted affects interpretation
   - [9, 6, 10] vs "9, 6, 10" vs "nine, six, ten"

---

## 🎯 What Inconsistency Detection Does

### Core Mechanism
Track every evaluation of every state, detect when same state gets very different scores.

```
State Evaluation History:

State [9, 6, 10]
├── Evaluation 1: score=0.8, depth=1, LLM_votes=["sure", "likely", "sure"]
├── Evaluation 2: score=0.6, depth=2, LLM_votes=["likely", "likely", "likely"]
├── Evaluation 3: score=0.2, depth=3, LLM_votes=["impossible", "likely", "impossible"]
└── Variance = 0.6 ⚠️  INCONSISTENT (threshold typically 0.3)
```

### Detection Algorithm

**Phase 1: Record**
```
Every time evaluate_state() is called:
  1. Save (state, score, depth, context)
  2. Check if we've seen this state before
  3. If yes → compute variance with previous evaluation(s)
```

**Phase 2: Flag**
```
If variance > threshold (e.g., 0.3):
  → State is INCONSISTENT
  → Log all scores and reasoning
  → Mark for investigation
```

**Phase 3: Report**
```
After search completes:
  → Generate inconsistency report
  → Show which states confused the LLM
  → Suggest actions (re-evaluate, lower score, ignore, etc.)
```

---

## 🔍 Detection Levels

### Level 1: Definite Inconsistency
```
Threshold: variance > 0.3

Example:
  Score 1: 0.9 (sure)
  Score 2: 0.1 (impossible)
  Variance: 0.8 ⚠️ CRITICAL

Action: Do NOT trust this state's evaluation
        Consider re-evaluating with different parameters
        Possible issue with state representation
```

### Level 2: Suspicious / Moderate Variance
```
Threshold: 0.15 < variance ≤ 0.3

Example:
  Score 1: 0.7 (likely)
  Score 2: 0.45 (moderate)
  Variance: 0.25 ⚠️ WATCH

Action: Monitor in future runs
        Consider if this state appears again
        May become inconsistent with more data
```

### Level 3: Consistent / Low Variance
```
Threshold: variance ≤ 0.15

Example:
  Score 1: 0.8 (sure)
  Score 2: 0.78 (sure)
  Variance: 0.02 ✓ GOOD

Action: Trust this evaluation
        LLM is consistent about this state
        Can use for beam selection
```

---

## 💡 Why Inconsistency Matters for ToT

### Problem 1: Cascading Errors
```
If parent state has unreliable evaluation:
  ├─ Wrong score → wrong priority in beam
  ├─ Wrong children selected → explore wrong branches
  └─ Waste API calls and search depth on dead ends

Example:
  [9, 6, 10] evaluated as 0.1 (impossible) at depth 2
    → Pruned from beam
    → Children never generated
    → BUT actual score should be 0.8 (likely)
    → Lost potentially good branch!
```

### Problem 2: Trust Issues
```
Which evaluation should we trust?

  State [9, 6, 10]
    Depth 1 → 0.8
    Depth 3 → 0.2
  
  Which is correct?
  
  Option A: First evaluation (depth 1)
           → May be biased by fresh context
  
  Option B: Most recent (depth 3)
           → May have forgotten good moves
  
  Option C: Average them (0.5)
           → Hides the inconsistency!
  
  Option D: Mark unreliable, avoid using
           → Safest but loses information
```

### Problem 3: LLM Confusion Detection
```
Some states confuse the LLM more than others

Inconsistent states reveal:
  ├─ Ambiguous problem states
  ├─ States where LLM reasoning is fragile
  ├─ Numbers that are hard to reason about together
  └─ Potentially adversarial examples

Knowing which states are "confusing" helps:
  ├─ Debug prompt engineering
  ├─ Improve state representation
  ├─ Choose better evaluation parameters
  └─ Design better heuristics for those states
```

### Problem 4: Search Efficiency
```
WITHOUT inconsistency detection:
  [9, 6, 10] evaluated 3 times
  Each time: API call + LLM reasoning
  Each time: Possibly different answer
  Total API cost: 3× evaluation + uncertainty

WITH inconsistency detection:
  [9, 6, 10] evaluated → flagged as inconsistent
  Next time same state appears: Don't re-evaluate
                                Use cached first result
                                Or use heuristic instead
  Total API cost: saved!
```

---

## 📊 Data to Track

### Per Evaluation
```python
{
    'state': [9, 6, 10],           # The game state
    'score': 0.8,                  # Final score
    'depth': 2,                    # Search depth when evaluated
    'llm_votes': ['sure', 'likely', 'sure'],  # Individual votes
    'reasoning': "6 and 10 close to 24",      # LLM reasoning
    'temperature': 0.7,            # Sampling temp used
    'model': 'gemini-1.5-flash',   # Which model
    'timestamp': datetime.now(),    # When evaluated
    'context_state_count': 4,      # How many numbers in context
}
```

### Per State (Aggregated)
```python
{
    'state': [9, 6, 10],
    'evaluation_count': 3,
    'evaluation_history': [
        {'score': 0.8, 'depth': 1, ...},
        {'score': 0.6, 'depth': 2, ...},
        {'score': 0.2, 'depth': 3, ...},
    ],
    'scores': [0.8, 0.6, 0.2],
    'min_score': 0.2,
    'max_score': 0.8,
    'variance': 0.6,
    'mean_score': 0.533,
    'std_dev': 0.283,
    'is_inconsistent': True,       # variance > 0.3
    'suspicious_level': 'CRITICAL',
}
```

---

## 🔧 How to Use Inconsistency Data

### Strategy 1: Filter & Trust Only First Evaluation
```
When state appears multiple times:
  → Always use first evaluation
  → Ignore all subsequent evaluations
  → Avoids variance entirely

Pros: Simple, consistent, no API waste
Cons: May miss updated information, early evaluations might be wrong
```

### Strategy 2: Re-evaluate Inconsistent States
```
If variance detected:
  → Flag state for re-evaluation
  → Use different temperature (lower = consistent)
  → Use better prompt
  → Compare new score with previous

Pros: Get better answer, improve LLM reliability
Cons: Extra API calls, adds latency
```

### Strategy 3: Conservative Scoring
```
If variance detected:
  → Lower the score confidence
  → Use min of all evaluations (pessimistic)
  → Or use mean ± std_dev (probabilistic)

Example:
  Scores: [0.8, 0.6, 0.2]
  Mean: 0.533
  
  Conservative: 0.2 (never promote unpredictable state)
  Probabilistic: 0.533 ± 0.283 (show uncertainty)
  Optimistic: 0.8 (trust best eval)
```

### Strategy 4: Heuristic Fallback
```
If state is inconsistent:
  → Don't trust LLM evaluation
  → Fall back to heuristic scoring
  → E.g., distance from 24, number balance, etc.

Example:
  [9, 6, 10] is inconsistent in LLM
  → Heuristic: |9*6 - 24| + |10 - 24| = 30 + 14 = 44
  → Score: 1 / (1 + 44) ≈ 0.02 (poor)
  
  Or: min(6+10-9, etc.) = 7 → better score
```

### Strategy 5: Exclusion
```
If state is highly inconsistent:
  → Remove from beam entirely
  → Don't explore its children
  → Accept loss of that branch
  
  Rationale: Unpredictable state → its children also unpredictable
```

---

## 📈 Metrics to Monitor

### Inconsistency Rate
```
Formula: % of re-evaluated states that are inconsistent

If high (>50%):
  → LLM evaluation is unreliable
  → Need better prompts or lower temperature
  → Consider simpler heuristic approach

If low (<10%):
  → LLM is stable and consistent
  → Can trust evaluation scores
  → Good sign for search quality
```

### Variance Distribution
```
Track histogram of variances:

Variance 0.0-0.1:  ████████████████ (16 states)  ← Good
Variance 0.1-0.2:  ██████████ (10 states)        ← Acceptable
Variance 0.2-0.3:  ███████ (7 states)            ← Watch
Variance 0.3+:     ██ (2 states)                 ← Problem

If tail is heavy → systematic issues with evaluation
```

### State-Specific Patterns
```
Which states are consistently inconsistent?

Examples:
  ├─ States with very large numbers (e.g., 1000, 0.001)
  ├─ States with few options (e.g., [20, 4] - only 2 numbers)
  ├─ States close to 24 (ambiguous - is it "sure" or "likely"?)
  └─ States with extreme ratios (e.g., [100, 0.1, 5])

Fix: Special handling or better prompts for these patterns
```

---

## 🎨 Example Report Output

```
================================================================================
INCONSISTENCY DETECTION REPORT
================================================================================

Total Evaluations:      142
Unique States:          87
Re-evaluations:         42 (29.6% of states re-evaluated)

Inconsistent States:    5 (11.9% of re-evals)
Suspicious States:      8 (19.0% of re-evals)
Consistent States:      29 (69.0% of re-evals)

================================================================================
⚠️  FLAGGED INCONSISTENCIES (5 states)
================================================================================

1. State [1000, 0.5, 2]
   Evaluations: 2
   Scores: [0.2, 0.9]
   Variance: 0.7 (CRITICAL)
   Depths: [1, 2]
   → Issue: Extreme range ratio (2000:1)
   → Recommendation: Lower the score, use heuristic instead

2. State [24, 5, 3]
   Evaluations: 2
   Scores: [0.3, 0.8]
   Variance: 0.5 (HIGH)
   Depths: [1, 3]
   → Issue: Contains 24 (ambiguous - alive or dead end?)
   → Recommendation: Special handling for premature 24

3. [Similar for states 3, 4, 5...]

================================================================================
🔍 SUSPICIOUS STATES (8 states with moderate variance)
================================================================================

1. [9, 6, 10]  - Variance: 0.25 - Monitor this
2. [12, 2, 8]  - Variance: 0.22 - Could become inconsistent
3. [Similar for 3-8...]

================================================================================
RECOMMENDATIONS
================================================================================

✅ Action 1: Temperature Adjustment
   → Re-evaluate flagged states with temperature=0.0 (deterministic)
   → Should reduce inconsistency

✅ Action 2: Prompt Engineering
   → Make VALUE_PROMPT more specific about edge cases
   → Example: "Large ratio states (>100:1) are usually impossible"

✅ Action 3: Heuristic Fallback
   → For states with variance > 0.5: use heuristic score only
   → Saves API calls + improves stability

✅ Action 4: Special State Handling
   → States with premature 24: always score low
   → States with extreme ratios: use conservative heuristic
   → Single-number states: heuristic only (no LLM eval needed)

✅ Action 5: Caching Strategy
   → Cache first evaluation of each state
   → On re-eval, compare with cache
   → Only use new eval if variance < 0.1

================================================================================
```

---

## 🚀 Integration with Your Solver

### Where to Add Inconsistency Tracking

```python
class Game24TreeOfThoughts:
    def __init__(self, ...):
        # ... other initialization ...
        
        # NEW: Add inconsistency detector
        self.inconsistency_detector = InconsistencyDetector(
            threshold=0.3
        )
    
    def evaluate_state(self, numbers, is_final=False):
        # ... evaluation logic ...
        
        score, eval_record = ... # get score
        
        # NEW: Track the evaluation
        inconsistency_info = self.inconsistency_detector.record_evaluation(
            state=numbers,
            score=score,
            depth=self.current_depth,
            reasoning=eval_record['reasoning']
        )
        
        # NEW: Act on inconsistency if detected
        if inconsistency_info and inconsistency_info['is_inconsistent']:
            print(f"⚠️  Inconsistency detected in {numbers}")
            # Option: lower score, re-eval, or ignore
        
        return score, eval_record
    
    def export_tree_to_json(self, filename):
        # ... existing export ...
        
        # NEW: Include inconsistency report
        tree_data['inconsistency_report'] = {
            'stats': self.inconsistency_detector.get_stats(),
            'inconsistent_states': self.inconsistency_detector.get_inconsistent_states(),
            'suspicious_states': self.inconsistency_detector.get_suspicious_states()
        }
```

---

## 📚 Connection to TiM Paper

### How It Relates to Think-in-Memory

**TiM Paper Insight:** 
> "Repeated reasoning over the same context produces different answers"

**Inconsistency Detection Application:**
- In TiM: Uses memory to AVOID re-reasoning (store insights once)
- In ToT: DETECTS when re-reasoning produces inconsistency
- In ToT with TiM: Could use memory + detection together:
  - Store evaluation reasoning as "insight"
  - On re-evaluation: retrieve stored insight instead of re-reasoning
  - Detect if new reasoning contradicts stored insight

---

## 🎯 Key Takeaways

| Aspect | Description |
|--------|-------------|
| **Problem** | Same state → different LLM scores = unreliable evaluation |
| **Detection** | Track all evaluations, compute variance |
| **Impact** | Cascading errors, wasted search, poor guidance |
| **Solution** | Flag inconsistent states, special handling |
| **Benefit** | Better debugging, improved search quality, API efficiency |
| **Integration** | Add to evaluate_state(), report in JSON export |
| **Priority** | Medium - nice to have, high value when debugging |

---

## 🔗 References

- Original TiM Paper: "Think-in-Memory" by Zhang et al.
- Related: "Inconsistency Detection in Reasoning Tasks" (not yet written)
- Connection: Beam Search, State Evaluation, LLM Reliability
