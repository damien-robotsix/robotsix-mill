"""Remove all `*_periodic: bool = Field(...)` fields from _settings_periodic.py
and update paired interval field descriptions to mention 0 = disabled."""

import re

path = "src/robotsix_mill/config/_settings_periodic.py"
with open(path) as f:
    content = f.read()
original = content

# Track all _periodic field line ranges
lines = content.split("\n")
remove_ranges = []

i = 0
while i < len(lines):
    line = lines[i]
    if "_periodic: bool = Field(" in line:
        start = i
        depth = line.count("(") - line.count(")")
        i += 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("(") - lines[i].count(")")
            i += 1
        # now at the closing ) line (or next line)
        # include preceding comment lines that are only for this field
        # go back to find the section header or previous field
        # Actually, let's just remove from start to i (inclusive of closing paren)
        remove_ranges.append((start, i))
    else:
        i += 1

# Remove ranges in reverse order so indices don't shift
for start, end in reversed(remove_ranges):
    # Extend start backwards to include preceding blank/comment lines
    # that are specifically part of this field's doc comment
    # (but stop at section headers or previous field)
    s = start
    while s > 0:
        prev = lines[s - 1].strip()
        if prev == "" or (prev.startswith("#") and not prev.startswith("# ---")):
            s -= 1
        else:
            break
    # Remove section
    del lines[s : end + 1]
    # Record: we need to update the next _interval_seconds field description
    # Find the next _interval_seconds field
    for j in range(s, len(lines)):
        if "_interval_seconds: int = Field(" in lines[j]:
            # Update its description
            k = j + 1
            while k < len(lines) and "description=" not in lines[k]:
                k += 1
            if k < len(lines):
                desc = lines[k]
                # Add "0 = disabled." if not already present
                if "0 = disabled" not in desc:
                    # Find the description text
                    m = re.search(r'description="([^"]*)"', desc)
                    if m:
                        old_text = m.group(1)
                        if not old_text.rstrip(".").endswith("0 = disabled"):
                            new_text = old_text.rstrip(".") + ". 0 = disabled."
                            lines[k] = desc.replace(
                                f'description="{old_text}"', f'description="{new_text}"'
                            )
            break

new_content = "\n".join(lines)
if new_content != original:
    with open(path, "w") as f:
        f.write(new_content)
    print(f"Removed {len(remove_ranges)} *_periodic fields")
else:
    print("No changes made")
