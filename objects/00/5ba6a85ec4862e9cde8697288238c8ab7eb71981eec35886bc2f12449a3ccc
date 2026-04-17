# =========================================================================
# Add this function to olympusrepo/core/diff.py
# after the existing diff_content() function
# =========================================================================

def diff_side_by_side(old: str, new: str) -> list[dict]:
    """
    Generate a side-by-side diff for web rendering.

    Returns a list of line-pair dicts:
    {
        "left":  {"text": str, "type": "removed"|"context"|"empty"},
        "right": {"text": str, "type": "added"|"context"|"empty"},
        "line_left":  int,  # 0 = no line number
        "line_right": int,
    }
    """
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    result   = []
    l_num    = 0
    r_num    = 0

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                l_num += 1
                r_num += 1
                result.append({
                    "left":       {"text": old_lines[i], "type": "context"},
                    "right":      {"text": new_lines[j], "type": "context"},
                    "line_left":  l_num,
                    "line_right": r_num,
                })

        elif op == "replace":
            old_chunk = old_lines[i1:i2]
            new_chunk = new_lines[j1:j2]
            max_len   = max(len(old_chunk), len(new_chunk))
            for k in range(max_len):
                has_old = k < len(old_chunk)
                has_new = k < len(new_chunk)
                if has_old: l_num += 1
                if has_new: r_num += 1
                result.append({
                    "left": {
                        "text": old_chunk[k] if has_old else "",
                        "type": "removed" if has_old else "empty",
                    },
                    "right": {
                        "text": new_chunk[k] if has_new else "",
                        "type": "added" if has_new else "empty",
                    },
                    "line_left":  l_num if has_old else 0,
                    "line_right": r_num if has_new else 0,
                })

        elif op == "delete":
            for i in range(i1, i2):
                l_num += 1
                result.append({
                    "left":       {"text": old_lines[i], "type": "removed"},
                    "right":      {"text": "",            "type": "empty"},
                    "line_left":  l_num,
                    "line_right": 0,
                })

        elif op == "insert":
            for j in range(j1, j2):
                r_num += 1
                result.append({
                    "left":       {"text": "",            "type": "empty"},
                    "right":      {"text": new_lines[j],  "type": "added"},
                    "line_left":  0,
                    "line_right": r_num,
                })

    return result


def diff_summary(old: str, new: str) -> dict:
    """
    Quick summary of changes between two strings.
    Returns {added, removed, changed_lines, is_binary}
    """
    try:
        old_lines = old.splitlines() if old else []
        new_lines = new.splitlines() if new else []
    except Exception:
        return {"added": 0, "removed": 0, "changed_lines": 0, "is_binary": True}

    added   = 0
    removed = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("replace", "delete"):
            removed += (i2 - i1)
        if op in ("replace", "insert"):
            added += (j2 - j1)

    return {
        "added":         added,
        "removed":       removed,
        "changed_lines": added + removed,
        "is_binary":     False,
    }
