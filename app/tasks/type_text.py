"""
Type text into element using AXValue attribute setting.
No keyboard simulation. Direct accessibility API.
"""

from ApplicationServices import (
    AXUIElementSetAttributeValue,
    kAXErrorSuccess,
    kAXValueAttribute,
)


def type_text(element, text: str) -> bool:
    """
    Type text into element by setting AXValue.

    Args:
        element: Raw AXUIElement reference
        text: Text string to type

    Returns:
        True on success

    Raises:
        RuntimeError on failure
    """
    if element is None:
        raise RuntimeError("Cannot type into None element")

    if not text:
        raise RuntimeError("Text cannot be empty")

    err = AXUIElementSetAttributeValue(element, kAXValueAttribute, text)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"AXSetValue failed with error code: {err}")

    return True


if __name__ == "__main__":
    from find_element import find_element

    element = find_element("Acellus", "Answer")
    if element:
        type_text(element, "Test answer text")
        print("Typed!")
    else:
        print("Element not found")
