"""
Crop screenshot region for VLM analysis.
Single function. No fallbacks. Uses Pillow.

Supports both:
- Direct bbox: [x, y, width, height]
- Element-based: find element in tree by role/name, use its visible_bbox
"""

import base64
from io import BytesIO
from PIL import Image
from typing import Optional


def find_element_bbox(tree: dict, role: str = None, name: str = None) -> Optional[list]:
    """
    Find element in tree by role/name and return its visible_bbox.

    Args:
        tree: Accessibility tree dict from capture_tree
        role: Element role to match (e.g., "AXImage")
        name: Element name to match (can be "" for empty names)

    Returns:
        visible_bbox as [x, y, width, height] or None if not found
    """
    def walk(node: dict) -> Optional[list]:
        if not isinstance(node, dict):
            return None

        # Check if this node matches
        node_role = node.get("role", "")
        node_name = node.get("name", "")

        role_matches = (role is None) or (node_role == role)
        name_matches = (name is None) or (node_name == name)

        if role_matches and name_matches:
            bbox = node.get("visible_bbox")
            if bbox and len(bbox) == 4:
                return bbox

        # Recurse into children
        for child in node.get("children", []):
            result = walk(child)
            if result:
                return result

        return None

    return walk(tree)


def crop_image_region(screenshot_b64: str, bbox: list) -> str:
    """
    Crop screenshot to specified bounding box region.

    Args:
        screenshot_b64: Full screenshot as base64 string
        bbox: Bounding box as [x, y, width, height]

    Returns:
        Cropped image region as base64 string

    Raises:
        RuntimeError on failure
    """
    if not screenshot_b64:
        raise RuntimeError("No screenshot provided")

    if not bbox or len(bbox) != 4:
        raise RuntimeError(f"Invalid bbox format: {bbox}. Expected [x, y, width, height]")

    x, y, width, height = bbox

    # Decode base64 to image
    img_bytes = base64.b64decode(screenshot_b64)
    img = Image.open(BytesIO(img_bytes))

    # Validate bbox within image bounds
    img_width, img_height = img.size
    if x < 0 or y < 0 or x + width > img_width or y + height > img_height:
        raise RuntimeError(
            f"Bbox {bbox} exceeds image bounds ({img_width}x{img_height})"
        )

    # Crop: PIL uses (left, upper, right, lower)
    cropped = img.crop((x, y, x + width, y + height))

    # Encode back to base64
    buffer = BytesIO()
    cropped.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


if __name__ == "__main__":
    # Test with a simple image
    from PIL import Image as PILImage

    # Create test image
    test_img = PILImage.new("RGB", (100, 100), color="blue")
    buffer = BytesIO()
    test_img.save(buffer, format="PNG")
    test_b64 = base64.b64encode(buffer.getvalue()).decode()

    # Crop center region
    cropped = crop_image_region(test_b64, [25, 25, 50, 50])
    print(f"Cropped image base64 length: {len(cropped)}")
