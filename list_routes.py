import re
with open('d:/MyCode/WorkAMA/apps/platform-api/src/workama_platform/modules/memory_vector.py') as f:
    for i, line in enumerate(f, 1):
        m = re.search(r'@router\.(get|post|delete)\("([^"]+)"', line)
        if m:
            print(f"{i:4d} {line.rstrip()}")
