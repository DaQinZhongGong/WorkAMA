import pathlib

f = pathlib.Path('d:/MyCode/WorkAMA/apps/platform-api/src/workama_platform/modules/memory_vector.py')
lines = f.read_text().splitlines(keepends=True)

# Find the start of /annotations block
start_idx = None
for i, line in enumerate(lines):
    if line.strip() == '@router.get("/annotations")':
        start_idx = i - 1 if i > 0 and lines[i-1].strip() == '' else i
        break

if start_idx is None:
    print('NOT FOUND')
    exit(1)

annotations_block = lines[start_idx:]
remaining = lines[:start_idx]

# Remove trailing blank lines from remaining
while remaining and remaining[-1].strip() == '':
    remaining.pop()

# Find insertion point before @router.get('/{vector_id}')
insert_idx = None
for i, line in enumerate(remaining):
    if '@router.get("/{vector_id}")' in line:
        insert_idx = i
        break

if insert_idx is None:
    print('INSERT POINT NOT FOUND')
    exit(1)

new_lines = remaining[:insert_idx] + annotations_block + ['\n'] + remaining[insert_idx:]
f.write_text(''.join(new_lines))
print('DONE')
