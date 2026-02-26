"""
Extract text from accessibility tree based on YAML criteria.
Single function. No fallbacks. Returns list of text strings.

Feb 2026: Scopes to AXWebArea subtree for browser platforms.
"""

from app.tasks.extract_question import _find_web_area


def extract_text(tree: dict, config: dict = None) -> list:
    """
    Extract text from tree nodes matching YAML criteria.

    Args:
        tree: Accessibility tree dict from capture_tree
        config: YAML extract.text config, e.g.:
            [{"role": "AXStaticText"}, {"role": "AXTextField", "parent_contains": "answer"}]
            If None, extracts all text from AXStaticText nodes

    Returns:
        List of text strings found

    Raises:
        RuntimeError on failure
    """
    if config is None:
        config = [{"role": "AXStaticText"}]

    texts = []

    def matches_criteria(node: dict, criteria: dict, parent_name: str = "", parent_role: str = "") -> bool:
        """Check if node matches extraction criteria."""
        # Role match (required)
        if "role" in criteria:
            if node.get("role") != criteria["role"]:
                return False

        # Parent role match (optional) - checks parent's role
        if "parent_role" in criteria:
            if parent_role != criteria["parent_role"]:
                return False

        # Parent contains match (optional) - checks parent's name
        if "parent_contains" in criteria:
            if criteria["parent_contains"].lower() not in parent_name.lower():
                return False

        # Contains match on text value (optional)
        if "contains" in criteria:
            text = node.get("value") or node.get("title") or node.get("description") or ""
            if criteria["contains"].lower() not in text.lower():
                return False

        # Min length filter (optional) - skip short nav labels, button text, etc.
        if "min_length" in criteria:
            text = node.get("value") or node.get("title") or node.get("description") or ""
            if len(str(text).strip()) < criteria["min_length"]:
                return False

        return True

    def walk_tree(node: dict, parent_name: str = "", parent_role: str = ""):
        """Recursively walk tree and collect matching text."""
        if not isinstance(node, dict):
            return

        node_name = node.get("name") or node.get("title") or ""
        node_role = node.get("role", "")

        # Check each criteria set
        for criteria in config:
            if matches_criteria(node, criteria, parent_name, parent_role):
                # Extract text value
                text = node.get("value") or node.get("title") or node.get("description")
                if text and len(str(text).strip()) > 1:  # Skip empty/bullet chars
                    texts.append(str(text).strip())
                break  # Don't double-add

        # Recurse into children
        for child in node.get("children", []):
            walk_tree(child, node_name, node_role)

    # Scope to web content area (excludes browser chrome)
    scoped = _find_web_area(tree)
    walk_tree(scoped)
    return texts


if __name__ == "__main__":
    # Test with mock tree
    mock_tree = {
        "role": "AXWindow",
        "name": "Lesson",
        "children": [
            {"role": "AXStaticText", "value": "Introduction to Banking"},
            {"role": "AXStaticText", "value": "Banks provide financial services."},
            {"role": "AXButton", "title": "Next"},
            {
                "role": "AXGroup",
                "name": "lesson-content",
                "children": [
                    {"role": "AXStaticText", "value": "A bank is a financial institution."},
                ]
            }
        ]
    }

    # Extract all text
    result = extract_text(mock_tree)
    print("All AXStaticText:", result)

    # Extract with parent filter
    result = extract_text(mock_tree, [{"role": "AXStaticText", "parent_contains": "lesson"}])
    print("In lesson-content:", result)
