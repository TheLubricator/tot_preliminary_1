"""
Isolated test for 2-number proposal system.
Tests propose_two_number() WITHOUT full tree search.
Saves all LLM responses to responses_2number.txt for inspection.
"""

import sys
sys.path.insert(0, r"g:\class codes\tot_preliminary_1\teacher")

from tot_prelim_gemini_COMPLETE import Game24TreeOfThoughts
import json
from datetime import datetime

# Test cases: various 2-number combinations
TEST_CASES = [
    ([15, 9], "Should return 15+9=24 (exact)"),
    ([12, 2], "Should return 12*2=24 (exact)"),
    ([10, 14], "Should return 10+14=24 (exact)"),
    ([6, 4], "Should return 6*4=24 (exact)"),
    ([3, 21], "Should return 3+21=24 (exact)"),
    ([7, 17], "Should return 7+17=24 (exact)"),
    ([11, 13], "Should return 11+13=24 (exact)"),
    ([8, 16], "Should return 8+16=24 (exact)"),
    ([20, 4], "Should return 20+4=24 (exact)"),
    ([30, 6], "Should return 30-6=24 (exact)"),
    ([5, 19], "Should return 5+19=24 (exact)"),
    ([1, 23], "Should return 1+23=24 (exact)"),
]

def run_isolated_test():
    """Run propose_two_number() for each test case."""
    solver = Game24TreeOfThoughts()
    
    # Track results
    results = {
        "test_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "test_cases": [],
        "parse_success_count": 0,
        "parse_failure_count": 0,
        "fallback_used_count": 0,
        "total_tests": len(TEST_CASES),
    }
    
    responses_log = []
    
    print(f"\n{'='*70}")
    print(f"ISOLATED 2-NUMBER PROPOSAL TEST")
    print(f"{'='*70}")
    print(f"Total test cases: {len(TEST_CASES)}\n")
    
    for i, (numbers, description) in enumerate(TEST_CASES, 1):
        print(f"\nTest {i}/{len(TEST_CASES)}: {numbers}")
        print(f"Description: {description}")
        print("-" * 70)
        
        try:
            # Call propose_two_number directly
            # Format: propose_two_number(current_numbers, original_input, history)
            proposals = solver.propose_two_number(
                current_numbers=numbers,
                original_input=str(numbers),  # Original puzzle input
                history=""  # No history since this is first step
            )
            
            print(f"Proposals returned: {len(proposals)}")
            
            if proposals:
                for j, proposal in enumerate(proposals, 1):
                    print(f"\nProposal {j}:")
                    print(f"  State: {proposal['state']}")
                    print(f"  Operation: {proposal.get('operation', 'N/A')}")
                    
                    # Check if it parsed successfully or used fallback
                    operation = proposal.get('operation', '')
                    if operation:
                        results["parse_success_count"] += 1
                        parse_status = "✅ PARSED"
                    else:
                        results["parse_failure_count"] += 1
                        parse_status = "❌ FAILED"
                    
                    results["test_cases"].append({
                        "input": numbers,
                        "description": description,
                        "proposal_state": proposal['state'],
                        "operation": operation,
                        "parse_status": parse_status,
                    })
            else:
                print("❌ No proposals returned!")
                results["test_cases"].append({
                    "input": numbers,
                    "description": description,
                    "proposal_state": None,
                    "operation": None,
                    "parse_status": "❌ EMPTY LIST",
                })
                results["parse_failure_count"] += 1
        
        except Exception as e:
            print(f"❌ ERROR: {e}")
            results["test_cases"].append({
                "input": numbers,
                "description": description,
                "proposal_state": None,
                "operation": None,
                "parse_status": f"❌ EXCEPTION: {str(e)}",
            })
            results["parse_failure_count"] += 1
    
    # Print summary
    print(f"\n{'='*70}")
    print("TEST SUMMARY")
    print(f"{'='*70}")
    print(f"Total tests: {results['total_tests']}")
    print(f"Parse successes: {results['parse_success_count']}")
    print(f"Parse failures: {results['parse_failure_count']}")
    success_rate = (results['parse_success_count'] / results['total_tests'] * 100) if results['total_tests'] > 0 else 0
    print(f"Success rate: {success_rate:.1f}%")
    print(f"{'='*70}\n")
    
    # Save results as JSON
    with open(r"g:\class codes\tot_preliminary_1\teacher\test_2number_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"Results saved to: test_2number_results.json")
    
    return results

if __name__ == "__main__":
    results = run_isolated_test()
    
    # Print success/failure for quick scanning
    print("\nQUICK STATUS:")
    for test in results["test_cases"]:
        status = "✅" if "PARSED" in test["parse_status"] else "❌"
        print(f"{status} {test['input']} → {test['operation']} ({test['parse_status']})")
