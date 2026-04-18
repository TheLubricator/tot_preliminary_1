#!/usr/bin/env python3
import json

with open(r'g:\class codes\tot_preliminary_1\teacher\puzzle_test_20260417_222946.json') as f:
    data = json.load(f)

def find_2num(node, depth=0):
    has_children = 0
    no_children = 0
    
    if isinstance(node.get('state'), list) and len(node.get('state', [])) == 2:
        if node.get('num_children', 0) > 0:
            has_children += 1
            print(f'2-NUM WITH children: {node["state"]} (id={node["id"]}, children={node["num_children"]})')
        else:
            no_children += 1
            print(f'2-NUM NO children: {node["state"]} (id={node["id"]})')
    
    for child_data in node.get('children', {}).values():
        h, n = find_2num(child_data, depth+1)
        has_children += h
        no_children += n
    
    return has_children, no_children

h, n = find_2num(data)
print(f'\nSummary:')
print(f'2-number WITH children: {h}')
print(f'2-number WITHOUT children: {n}')
if n == 0 and h > 0:
    print('✅ SUCCESS! All 2-number states create children!')
elif n > 0:
    print(f'⚠️  PROBLEM: {n} 2-number states have NO children')
