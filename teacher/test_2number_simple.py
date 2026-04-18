"""
Simple test runner that imports from notebook kernel via nbclient.
Tests propose_two_number() WITHOUT full tree search.
"""

import sys
import json
from datetime import datetime
from pathlib import Path

# Add notebook path
notebook_path = Path(r"g:\class codes\tot_preliminary_1\teacher\tot_prelim_gemini_COMPLETE.ipynb")

# Read and execute all code cells from the notebook
import nbformat
from nbconvert.preprocessors import ExecutePreprocessor

# Load the notebook
with open(notebook_path) as f:
    nb = nbformat.read(f, as_version=4)

# Execute all cells to get the classes defined
ep = ExecutePreprocessor(timeout=300)
try:
    ep.preprocess(nb, {'metadata': {'path': notebook_path.parent}})
    print("✅ Notebook executed successfully")
except Exception as e:
    print(f"⚠️ Notebook execution had issues: {e}")

# Now get the classes from the notebook's namespace
# We'll need to extract them differently

print("\n" + "="*70)
print("ISOLATED 2-NUMBER PROPOSAL TEST")
print("="*70)
print("Note: Run this from within the notebook after executing all cells, or use nbconvert\n")

print("The notebook kernel should have:")
print("  - Game24TreeOfThoughts class")
print("  - TreeNode class")
print("  - SafeAgentSandbox class")
print("\nTo test propose_two_number(), run the following in a notebook cell:\n")

test_code = '''
# Test code to run in notebook
test_cases = [
    ([15, 9], "Should return 15+9=24"),
    ([12, 2], "Should return 12*2=24"),
    ([10, 14], "Should return 10+14=24"),
    ([6, 4], "Should return 6*4=24"),
    ([3, 21], "Should return 3+21=24"),
]

results = []
for numbers, description in test_cases:
    print(f"\\nTesting: {numbers} - {description}")
    proposals = solver.propose_two_number(
        current_numbers=numbers,
        original_input=str(numbers),
        history=""
    )
    
    if proposals:
        for p in proposals:
            print(f"  ✅ Result: {p['state']}, Operation: {p.get('operation', 'N/A')}")
            results.append({
                'input': numbers,
                'state': p['state'],
                'operation': p.get('operation'),
                'success': True
            })
    else:
        print(f"  ❌ No proposals returned")
        results.append({
            'input': numbers,
            'state': None,
            'operation': None,
            'success': False
        })

print(f"\\n{'='*70}")
print(f"Results: {sum(1 for r in results if r['success'])}/{len(results)} successful")
print(f"{'='*70}")

# Save results
import json
with open('test_2number_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print("Results saved to: test_2number_results.json")
'''

print(test_code)
