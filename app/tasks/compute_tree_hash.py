"""
Compute hash of accessibility tree for change detection.
Used to detect if screen changed after an action.

ALGORITHM MUST MATCH SPARK SIDE:
  spark/tasks/consultation_state.py: compute_tree_hash()
  spark/tasks/validate_action.py: _compute_tree_hash()

Algorithm: flatten tree to sorted "role:name" strings, join with "|", SHA256[:16].
"""

import hashlib


def compute_tree_hash(tree: dict) -> str:
    """
    Compute SHA256 hash of tree for change detection.

    Flattens tree to sorted "role:name" pairs, joins with "|", SHA256[:16].
    Matches Spark's algorithm exactly.

    Args:
        tree: Accessibility tree dict

    Returns:
        SHA256 hex string (first 16 chars for brevity)

    Note:
        Used to detect screen changes after action execution.
        Before/after hash comparison tells Spark Claude if action worked.
    """

    def extract_relevant(node: dict) -> list:
        """Flatten tree to role:name string pairs."""
        result = []
        role = node.get("role", "")
        name = node.get("name", "")
        if role or name:
            result.append(f"{role}:{name}")
        for child in node.get("children", []):
            result.extend(extract_relevant(child))
        return result

    relevant = sorted(extract_relevant(tree))
    content = "|".join(relevant)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


if __name__ == "__main__":
    # Test with sample tree
    sample_tree = {
        "role": "AXWindow",
        "name": "Test",
        "children": [
            {"role": "AXButton", "name": "START"},
            {"role": "AXStaticText", "name": "Classes"}
        ]
    }
    print(f"Hash: {compute_tree_hash(sample_tree)}")
