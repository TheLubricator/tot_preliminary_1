#!/usr/bin/env python3
import json

with open(r'g:\class codes\tot_preliminary_1\teacher\puzzle_test_20260417_222946.json') as f:
    data = json.load(f)

print(f'Solutions found: {len(data.get("solutions", []))}')
print(f'Total nodes: {data.get("metadata", {}).get("statistics", {}).get("total_nodes", "?")}')
print(f'API calls: {data.get("metadata", {}).get("statistics", {}).get("api_calls", "?")}')

# Count solutions in tree
def count_solutions(node, depth=0):
    count = 0
    if node.get('is_solution'):
        count += 1
        print(f'  Solution at depth {depth}: {node["state"]}')
    for child in node.get('children', {}).values():
        count += count_solutions(child, depth+1)
    return count

print("\nSearching for solutions in tree...")
sols = count_solutions(data)
print(f'Solutions found in tree: {sols}')
