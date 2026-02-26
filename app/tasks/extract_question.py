"""
Extract question text and answer options from accessibility tree.
Uses YAML extract section to find question and options.
Returns structured dict for answer generation.

Phase 6 task file.

Feb 2026: Scopes extraction to AXWebArea subtree for browser platforms.
Without this, Chrome toolbar buttons/tabs get picked up as quiz options.
"""


def _find_web_area(node: dict) -> dict:
    """
    Find AXWebArea subtree in Chrome/browser accessibility tree.
    Returns the AXWebArea node if found, otherwise returns the original tree.
    This scopes extraction to web content only, excluding browser chrome
    (tabs show as AXRadioButton in Chrome's tree — must exclude them).
    """
    if not isinstance(node, dict):
        return node
    if node.get("role") == "AXWebArea":
        return node
    for child in node.get("children", []):
        result = _find_web_area(child)
        if result.get("role") == "AXWebArea":
            return result
    return node  # No AXWebArea found, return original


def extract_question(tree: dict, extract_config: dict) -> dict:
    """
    Extract question text and answer options from tree.

    Args:
        tree: Accessibility tree dict from capture_tree
        extract_config: YAML extract section, e.g.:
            {
                "question": {"role": "AXStaticText", "contains": "?"},
                "options": {
                    "role": "AXButton",
                    "exclude_titles": ["Back", "Close", ...]
                },
                "text": [{"role": "AXStaticText", ...}]
            }

    Returns:
        {
            "question_text": "What is the primary function...?",
            "options": ["Hold deposits", "Print money", ...],
            "reference_texts": ["Banks hold deposits...", ...],
            "question_type": "choice" or "fill_blank"
        }

    Raises:
        RuntimeError if question cannot be extracted
    """
    # Scope to web content area (excludes Chrome toolbar/tabs)
    scoped_tree = _find_web_area(tree)

    question_text = ""
    options = []
    reference_texts = []

    # Extract question text
    q_config = extract_config.get("question")
    if q_config:
        q_role = q_config.get("role", "AXStaticText")
        q_contains = q_config.get("contains", "")
        question_text = _find_question_text(scoped_tree, q_role, q_contains)
        # Fallback: fill-in-the-blank questions use "___" instead of "?"
        if not question_text and q_contains == "?":
            question_text = _find_question_text(scoped_tree, q_role, "___")
        # Fallback: imperative prompts like "Complete the statement." or "Fill in the blank."
        if not question_text and q_contains == "?":
            for pattern in ["Complete the", "Fill in the", "Select the", "Match the"]:
                question_text = _find_question_text(scoped_tree, q_role, pattern)
                if question_text:
                    break

    # Extract answer options (for solve_choice)
    opt_config = extract_config.get("options")
    if opt_config:
        opt_role = opt_config.get("role", "AXButton")
        exclude = opt_config.get("exclude_titles", [])
        options = _find_options(scoped_tree, opt_role, exclude)

    # Extract reference/context text
    text_config = extract_config.get("text")
    if text_config:
        reference_texts = _extract_reference_texts(scoped_tree, text_config)

    # Detect text input fields (Coursera reflection quizzes have radio + text area)
    text_fields = _find_text_fields(scoped_tree)

    # Determine question type
    if options and text_fields:
        question_type = "choice_with_text"
    elif options:
        question_type = "choice"
    else:
        question_type = "fill_blank"

    # If no explicit question found, try to build from reference texts
    if not question_text and reference_texts:
        # Look for any text containing "?" or "___" in reference texts
        for text in reference_texts:
            if "?" in text or "___" in text:
                question_text = text
                break

    # Fallback: if we have options but no question, compose from reference texts
    # that look like instructions or incomplete statements (e.g., "Complete the statement."
    # followed by "An element is defined by the number of ...")
    if not question_text and reference_texts and options:
        imperative_patterns = ["complete", "fill in", "select", "match", "choose",
                               "identify", "determine", "defined by", "is a"]
        instruction_parts = []
        for text in reference_texts:
            text_lower = text.lower()
            if any(p in text_lower for p in imperative_patterns):
                instruction_parts.append(text)
            elif text.rstrip().endswith((" ", "\u2026", "...")):
                # Trailing space or ellipsis suggests incomplete statement
                instruction_parts.append(text)
        if instruction_parts:
            question_text = " ".join(instruction_parts)

    if not question_text:
        raise RuntimeError("Could not extract question text from tree")

    return {
        "question_text": question_text,
        "options": options,
        "reference_texts": reference_texts,
        "question_type": question_type,
        "has_text_field": len(text_fields) > 0,
        "text_field_count": len(text_fields),
    }


def _find_text_fields(node: dict) -> list:
    """Find text input fields (AXTextArea, AXTextField) in tree.
    Coursera reflection quizzes have radio buttons AND a text area."""
    fields = []

    def walk(n: dict):
        if not isinstance(n, dict):
            return
        role = n.get("role", "")
        if role in ("AXTextArea", "AXTextField"):
            title = n.get("title") or n.get("description") or n.get("name") or ""
            fields.append({"role": role, "title": str(title).strip()})
        for child in n.get("children", []):
            walk(child)

    walk(node)
    return fields


def _find_question_text(node: dict, role: str, contains: str) -> str:
    """Find question text node matching role and contains criteria."""
    if not isinstance(node, dict):
        return ""

    node_role = node.get("role", "")
    text = node.get("value") or node.get("title") or node.get("name") or node.get("description") or ""
    text = str(text).strip()

    if node_role == role and text:
        if not contains or contains.lower() in text.lower():
            return text

    # Recurse
    for child in node.get("children", []):
        result = _find_question_text(child, role, contains)
        if result:
            return result

    return ""


def _find_options(node: dict, role: str, exclude: list) -> list:
    """Find answer option elements matching role, excluding non-answer buttons."""
    options = []
    exclude_lower = [e.lower() for e in exclude]

    def walk(n: dict):
        if not isinstance(n, dict):
            return
        n_role = n.get("role", "")
        title = n.get("title") or n.get("name") or n.get("value") or n.get("description") or ""
        title = str(title).strip()

        if n_role == role and title:
            if title.lower() not in exclude_lower:
                options.append(title)

        for child in n.get("children", []):
            walk(child)

    walk(node)
    # Deduplicate while preserving order (tree may have duplicate nodes)
    seen = set()
    unique = []
    for opt in options:
        if opt not in seen:
            seen.add(opt)
            unique.append(opt)
    return unique


def _extract_reference_texts(tree: dict, config: list) -> list:
    """Extract reference/context texts using same logic as extract_text.py."""
    texts = []

    def matches(node: dict, criteria: dict, parent_name: str, parent_role: str) -> bool:
        if "role" in criteria and node.get("role") != criteria["role"]:
            return False
        if "parent_role" in criteria and parent_role != criteria["parent_role"]:
            return False
        if "parent_contains" in criteria:
            if criteria["parent_contains"].lower() not in parent_name.lower():
                return False
        return True

    def walk(node: dict, parent_name: str = "", parent_role: str = ""):
        if not isinstance(node, dict):
            return
        name = node.get("name") or node.get("title") or ""
        role = node.get("role", "")

        for criteria in config:
            if matches(node, criteria, parent_name, parent_role):
                text = node.get("value") or node.get("title") or node.get("description")
                if text and len(str(text).strip()) > 1:
                    texts.append(str(text).strip())
                break

        for child in node.get("children", []):
            walk(child, name, role)

    walk(tree)
    return texts


if __name__ == "__main__":
    # Test with mock tree
    mock_tree = {
        "role": "AXWindow",
        "children": [
            {"role": "AXStaticText", "value": "Choose the correct answer."},
            {
                "role": "AXGroup",
                "name": "question-area",
                "children": [
                    {"role": "AXStaticText", "value": "What is the primary function of a bank?"},
                ]
            },
            {"role": "AXButton", "title": "Hold deposits and make loans"},
            {"role": "AXButton", "title": "Print currency"},
            {"role": "AXButton", "title": "Set tax rates"},
            {"role": "AXButton", "title": "Back"},
            {"role": "AXButton", "title": "Menu"},
        ]
    }

    config = {
        "question": {"role": "AXStaticText", "contains": "?"},
        "options": {
            "role": "AXButton",
            "exclude_titles": ["Back", "Close", "Menu", "Skip"]
        },
        "text": [{"role": "AXStaticText"}]
    }

    result = extract_question(mock_tree, config)
    print(f"Question: {result['question_text']}")
    print(f"Options: {result['options']}")
    print(f"Reference: {result['reference_texts']}")
    print(f"Type: {result['question_type']}")
