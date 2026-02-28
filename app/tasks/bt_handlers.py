"""
Behavior Tree Action Handlers - All registered action handlers.

Each handler is a closure inside register_all_handlers to enable lazy imports
(Mac-specific dependencies like Quartz are only imported when needed).
"""

import logging
import time

from app.tasks.bt_core import ExecutionContext, btlog
from app.tasks.bt_helpers import (
    _find_menu_subtree, _extract_menu_items, _find_preceding_label,
    _find_assessment_questions, _find_web_area,
)

logger = logging.getLogger("taey-ed")


# =========================================================================
# Action Handlers Registration
# =========================================================================

def register_all_handlers(ctx: ExecutionContext):
    """Register all action handlers. Imports are inside functions
    so this file can be tested without Mac-specific dependencies."""

    # --- click: click an element (from blackboard or by finding it) ---
    def handle_click(ctx, params):
        from app.tasks.find_element import find_element
        from app.tasks.click_element import click_element, StaleElementError

        element = params.get("element")
        strategy = params.get("strategy", "mouse_click")

        # If element is a dict from find_all (has "element" key),
        # RE-FIND the element fresh by its description text.
        # AXUIElement refs go stale after discovery clicks (off-by-one bug).
        if isinstance(element, dict) and "description" in element:
            desc = element.get("description", "")
            btlog(f"click: re-finding popup by description='{desc[:60]}'")
            element = find_element(ctx.app_name, desc, role="AXPopUpButton",
                                    match_mode="contains")
            if not element:
                btlog(f"click: re-find by description FAILED — element gone, not using stale ref")
                return {"success": False, "action": "click", "error": f"Element re-find failed for '{desc[:60]}'"}

        # If element is a string, find it
        if element is None or isinstance(element, str):
            target = params.get("target", element or "")
            role = params.get("role")
            match_mode = params.get("match_mode", "exact")
            element = find_element(ctx.app_name, target, role=role, match_mode=match_mode)

        if element is None:
            btlog(f"click: element not found")
            return None

        try:
            click_element(element, strategy=strategy)
        except StaleElementError as e:
            btlog(f"click: stale element (off-screen/overlay): {e}")
            return None
        btlog(f"click: clicked element with strategy={strategy}")
        return {"success": True}

    # --- find_and_click: find by text/role with fallbacks, then click ---
    def handle_find_and_click(ctx, params):
        from app.tasks.find_element import find_element, find_all_elements
        from app.tasks.click_element import click_element, StaleElementError

        target = params.get("target", "")
        role = params.get("role")
        strategy = params.get("strategy", "mouse_click")
        match_mode = params.get("match_mode", "exact")

        btlog(f"find_and_click: looking for '{target[:60]}' role={role}")

        element = None

        # For AXMenuItem: use find_all_elements (web-area scoped) to avoid
        # finding Chrome menu bar items instead of dropdown items
        if role == "AXMenuItem" and target:
            results = find_all_elements(ctx.app_name, role="AXMenuItem")
            btlog(f"find_and_click: web-area AXMenuItems found: {len(results)}")
            for el, desc in results:
                btlog(f"  AXMenuItem desc='{desc[:80]}'")
                if desc == target or target in desc or desc in target:
                    element = el
                    btlog(f"find_and_click: MATCHED AXMenuItem desc='{desc[:60]}'")
                    break
            if not element:
                btlog(f"find_and_click: NO AXMenuItem matched target='{target[:60]}'")
                btlog(f"find_and_click: falling through to find_element")

        # Standard find_element path (for non-menu items)
        if not element:
            element = find_element(ctx.app_name, target, role=role, match_mode=match_mode)

        if not element:
            logger.error(f"find_and_click: '{target[:60]}' not found (role={role})")
            return None

        try:
            click_element(element, strategy=strategy)
        except StaleElementError as e:
            btlog(f"find_and_click: stale element (off-screen/overlay): {e}")
            return None
        logger.info(f"find_and_click: clicked '{target[:60]}'")
        return {"success": True, "target": target}

    # --- find_and_type: find text field + type ---
    def handle_find_and_type(ctx, params):
        from app.tasks.find_element import find_element, find_all_elements
        from app.tasks.click_element import click_element, StaleElementError
        from app.tasks.type_text import type_text

        target = params.get("target", "")
        role = params.get("role")
        text = params.get("text", "")
        focus_strategy = params.get("focus_strategy")

        # When target is empty and searching by role only, use web-area-scoped
        # find_all_elements to avoid hitting browser chrome (e.g., address bar
        # is an AXTextField at depth ~10, before web content at depth ~31).
        if not target and role:
            results = find_all_elements(ctx.app_name, role=role)
            element = results[0][0] if results else None
        else:
            element = find_element(ctx.app_name, target, role=role)
        if not element:
            logger.error(f"find_and_type: field not found (role={role})")
            return None

        if focus_strategy:
            try:
                click_element(element, strategy=focus_strategy)
            except StaleElementError as e:
                btlog(f"find_and_type: stale element during focus click: {e}")
                return None
            time.sleep(0.3)

        type_text(element, text)
        time.sleep(0.5)
        return {"success": True}

    # --- find_all: find all matching elements, return enriched list ---
    def handle_find_all(ctx, params):
        from app.tasks.find_element import find_all_elements
        from app.tasks.capture_tree import capture_tree

        role = params.get("role", "")
        desc_contains = params.get("description_contains")

        results = find_all_elements(ctx.app_name, role=role,
                                     description_contains=desc_contains)

        # Capture tree for label finding
        tree = capture_tree(ctx.app_name)

        items = []
        for element, desc in results:
            label = _find_preceding_label(tree, desc)
            items.append({
                "element": element,
                "description": desc,
                "popup_desc": desc,  # Alias for LLM compatibility
                "label": label,
            })

        logger.info(f"find_all: {len(items)} elements (role={role})")
        return items

    # --- wait: sleep ---
    def handle_wait(ctx, params):
        seconds = params.get("seconds", 1.0)
        time.sleep(seconds)
        return {"success": True}

    # --- extract_question: parse question/options from tree ---
    def handle_extract_question(ctx, params):
        from app.tasks.capture_tree import capture_tree
        from app.tasks.extract_question import extract_question

        # Build effective extract config: BT params override ctx.extract_config.
        # Gemini Pro builds per-question params (e.g. parent_contains: "Question 1")
        # that are more specific than the top-level extract_config.
        effective_config = dict(ctx.extract_config) if ctx.extract_config else {}
        if params.get("question"):
            effective_config["question"] = params["question"]
        if params.get("options"):
            effective_config["options"] = params["options"]
        if params.get("text"):
            effective_config["text"] = params["text"]

        if not effective_config:
            btlog("extract_question: FAIL - no extract_config and no BT params")
            return {"success": False, "error": "no extract_config"}

        btlog(f"extract_question: config={effective_config}")

        tree = capture_tree(ctx.app_name)
        try:
            q_data = extract_question(tree, effective_config)
        except Exception as e:
            btlog(f"extract_question: FAIL - exception: {e}")
            return {"success": False, "error": str(e)}

        # Empty question = extraction failed. Return FAILURE, not empty SUCCESS.
        if not q_data.get("question_text"):
            btlog(f"extract_question: FAIL - empty question_text")
            return {"success": False, "error": "no question found in tree"}

        q_data["success"] = True
        return q_data

    # --- discover_menu: capture tree, find menu items by role ---
    def handle_discover_menu(ctx, params):
        from app.tasks.capture_tree import capture_tree

        role = params.get("role", "AXMenuItem")
        tree = capture_tree(ctx.app_name)

        # Scope to AXWebArea FIRST to avoid capturing macOS system menus
        web_area = _find_web_area(tree)

        # Find AXMenu within web area (popup dropdown menu)
        menu_subtree = _find_menu_subtree(web_area)
        if menu_subtree:
            logger.info("discover_menu: found AXMenu in web area")
        else:
            # Fallback: search full tree but skip menus with system items
            menu_subtree = _find_menu_subtree(tree)
            if menu_subtree:
                # Sanity check: system menus have items like "About This Mac"
                test_items = _extract_menu_items(menu_subtree, role)
                system_indicators = ["About This Mac", "System Settings", "Force Quit", "Log Out"]
                if any(si in test_items for si in system_indicators):
                    logger.warning("discover_menu: found system menu, ignoring")
                    menu_subtree = web_area
                else:
                    logger.info("discover_menu: found AXMenu in full tree")
            else:
                menu_subtree = web_area
                logger.warning("discover_menu: no AXMenu found, using AXWebArea")

        items = _extract_menu_items(menu_subtree, role)
        logger.info(f"discover_menu: {len(items)} items found")
        return items

    # --- press_escape: dismiss popup ---
    def handle_press_escape(ctx, params):
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap
        )
        event_down = CGEventCreateKeyboardEvent(None, 53, True)
        CGEventPost(kCGHIDEventTap, event_down)
        time.sleep(0.05)
        event_up = CGEventCreateKeyboardEvent(None, 53, False)
        CGEventPost(kCGHIDEventTap, event_up)
        return {"success": True}

    # --- send_to_llm: build payload, call /generate, return result ---
    def handle_send_to_llm(ctx, params):
        from app.tasks.call_spark import call_spark
        from app.tasks.build_kb_context import build_kb_context
        from app.tasks.capture_macapptree import capture_screenshot
        import base64

        question = params.get("question", "")
        question_type = params.get("question_type", "solve_choice")
        options = params.get("options")
        items = params.get("items")
        context_texts = params.get("context") or []
        image_descriptions = params.get("image_descriptions")
        has_text_field = params.get("has_text_field", False)

        # Build KB context
        kb_context = build_kb_context(ctx.platform, ctx.course_id, question)
        all_context = list(context_texts) + kb_context if context_texts else kb_context

        payload = {
            "question": question,
            "question_type": question_type,
            "context": all_context if all_context else None,
        }
        if options:
            payload["options"] = options
        if image_descriptions:
            payload["image_descriptions"] = image_descriptions
        if has_text_field:
            payload["has_text_field"] = True

        # For solve_complex: include screenshot so Gemini can SEE the screen
        # (diagrams, checked boxes, layout context that text extraction misses)
        if question_type == "solve_complex":
            try:
                screenshot_bytes = capture_screenshot(ctx.app_name)
                payload["screenshot_b64"] = base64.b64encode(screenshot_bytes).decode("utf-8")
                btlog(f"send_to_llm: attached screenshot for solve_complex ({len(screenshot_bytes)} bytes)")
            except Exception as e:
                btlog(f"send_to_llm: screenshot capture failed (continuing without): {e}")

        # For matching: convert popup dicts to LLM-friendly format
        if items:
            llm_items = []
            for item in items:
                if isinstance(item, dict):
                    llm_items.append({
                        "label": item.get("label", ""),
                        "popup_desc": item.get("popup_desc", item.get("description", "")),
                        "options": item.get("options", []),
                    })
                else:
                    llm_items.append(item)
            payload["items"] = llm_items

        result = call_spark("/api/v1/generate", payload)
        btlog(f"send_to_llm: answer={result.get('answer', '')[:60]} success={result.get('success')} matches_keys={list(result.get('matches', {}).keys()) if result.get('matches') else 'None'}")

        # Validate response has expected keys based on question_type
        if result.get("success") and question_type in ("solve_choice", "solve", "solve_complex", "navigate"):
            if not result.get("answer"):
                btlog(f"send_to_llm: FAIL - success=True but no 'answer' key for {question_type}")
                return {"success": False, "error": f"LLM returned no answer for {question_type}"}
        elif result.get("success") and question_type == "solve_checkbox":
            if not result.get("selected"):
                btlog(f"send_to_llm: FAIL - success=True but no 'selected' key for solve_checkbox")
                return {"success": False, "error": "LLM returned no selected items for solve_checkbox"}
        elif result.get("success") and question_type == "solve_matching":
            if not result.get("matches"):
                btlog(f"send_to_llm: FAIL - success=True but no 'matches' key for solve_matching")
                return {"success": False, "error": "LLM returned no matches for solve_matching"}

        return result

    # --- lookup_match: dict lookup with partial matching ---
    def handle_lookup_match(ctx, params):
        matches = params.get("matches", {})
        key = params.get("key", "")

        btlog(f"lookup_match: key='{key}', matches_keys={list(matches.keys()) if matches else 'None'}")

        if not matches or not key:
            btlog(f"lookup_match: FAIL empty matches={bool(matches)} or key='{key}'")
            return None

        # Direct match
        result = matches.get(key)
        if result:
            return result

        # Partial match
        key_lower = key.lower()
        for mkey, mval in matches.items():
            if mkey.lower() in key_lower or key_lower in mkey.lower():
                return mval

        logger.warning(f"lookup_match: no match for '{key}' in {list(matches.keys())}")
        return None

    # --- store_qa: save Q&A pair ---
    def handle_store_qa(ctx, params):
        from app.tasks.store_qa import store_qa

        question = params.get("question", "")
        answer = params.get("answer", "")
        question_type = params.get("question_type", "unknown")
        store_qa(ctx.platform, ctx.course_id, question, str(answer), question_type)
        return {"success": True}

    # --- solve_assessment_page: solve radio + checkbox questions ONE AT A TIME ---
    def handle_solve_assessment_page(ctx, params):
        from app.tasks.capture_tree import capture_tree
        from app.tasks.find_element import find_element
        from app.tasks.click_element import click_element
        from app.tasks.call_spark import call_spark
        from app.tasks.build_kb_context import build_kb_context

        tree = capture_tree(ctx.app_name)
        web_area = _find_web_area(tree)
        btlog(f"solve_assessment: web_area role={web_area.get('role','')} name={web_area.get('name','')[:60]}")

        # Find question containers (AXGroup named "Question N ...")
        questions = _find_assessment_questions(web_area)
        btlog(f"solve_assessment: found {len(questions)} questions")

        if not questions:
            # Debug: dump top-level roles to understand tree
            btlog("solve_assessment: NO QUESTIONS FOUND - dumping tree roles:")
            def _dump(n, d=0):
                if not isinstance(n, dict) or d > 6:
                    return
                r = n.get("role", "")
                nm = n.get("name", "")[:50]
                if r:
                    btlog(f"  {'  '*d}{r}: {nm}")
                for c in n.get("children", []):
                    _dump(c, d+1)
            _dump(web_area)
            return None

        # Process EACH question individually
        for i, q in enumerate(questions):
            q_type = q["type"]
            q_text = q["question"]
            options = q["options"]
            btlog(f"solve_assessment: === Q{i+1}/{len(questions)} ({q_type}) ===")
            btlog(f"  question: {q_text[:80]}")
            for j, opt in enumerate(options):
                btlog(f"  opt {j}: {opt[:70]}")

            # Build KB context per-question (keyword search on actual question text)
            kb_context = build_kb_context(ctx.platform, ctx.course_id, q_text)
            if kb_context:
                btlog(f"  KB context: {len(kb_context)} items")

            if q_type == "radio":
                # Single choice - use proven solve_choice
                result = call_spark("/api/v1/generate", {
                    "question": q_text,
                    "question_type": "solve_choice",
                    "options": options,
                    "context": kb_context if kb_context else None,
                })

                if result.get("success"):
                    answer = result.get("answer", "")
                    btlog(f"  LLM answer: {answer[:60]}")
                    el = find_element(ctx.app_name, answer, role="AXRadioButton",
                                      match_mode="contains")
                    if el:
                        click_element(el, strategy="mouse_click")
                        btlog(f"  CLICKED radio button")
                    else:
                        btlog(f"  WARN: radio not found '{answer[:60]}'")
                else:
                    btlog(f"  WARN: LLM failed: {result.get('error')}")

                time.sleep(0.5)

            elif q_type == "checkbox":
                # Multi-select - use solve_checkbox
                result = call_spark("/api/v1/generate", {
                    "question": q_text,
                    "question_type": "solve_checkbox",
                    "options": options,
                    "context": kb_context if kb_context else None,
                })

                if result.get("success"):
                    selected = result.get("selected", [])
                    btlog(f"  LLM selected {len(selected)} of {len(options)}")
                    for sel_text in selected:
                        btlog(f"  clicking: {sel_text[:60]}")
                        el = find_element(ctx.app_name, sel_text, role="AXCheckBox",
                                          match_mode="contains")
                        if el:
                            click_element(el, strategy="mouse_click")
                            btlog(f"  CLICKED checkbox")
                        else:
                            btlog(f"  WARN: checkbox not found '{sel_text[:60]}'")
                        time.sleep(0.3)
                else:
                    btlog(f"  WARN: LLM failed: {result.get('error')}")

                time.sleep(0.5)

        btlog(f"solve_assessment: all {len(questions)} questions processed")
        return {"success": True}

    # --- video_poll: sleep then signal re-check (always 30s) ---
    def handle_video_poll(ctx, params):
        # Hardcoded 30s: check every 30s if screen changed. Never override.
        # Videos range from 1min to 1hr+. 30s is human-paced checking.
        if ctx.stop_event:
            ctx.stop_event.wait(timeout=30)
            if ctx.stop_event.is_set():
                btlog("video_poll: interrupted by stop_event")
                return {"success": False}
        else:
            time.sleep(30)
        return {"success": True, "continue_loop": True}

    # --- press_key: generic keypress (Enter, Tab, arrows, etc.) ---
    def handle_press_key(ctx, params):
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
            kCGHIDEventTap, kCGEventFlagMaskShift, kCGEventFlagMaskCommand,
            kCGEventFlagMaskAlternate, kCGEventFlagMaskControl,
        )

        KEY_CODES = {
            "return": 36, "enter": 36,
            "tab": 48,
            "escape": 53,
            "space": 49,
            "backspace": 51, "delete": 51,
            "up": 126, "down": 125, "left": 123, "right": 124,
            "home": 115, "end": 119,
            "pageup": 116, "pagedown": 121,
        }
        MODIFIER_FLAGS = {
            "shift": kCGEventFlagMaskShift,
            "cmd": kCGEventFlagMaskCommand, "command": kCGEventFlagMaskCommand,
            "alt": kCGEventFlagMaskAlternate, "option": kCGEventFlagMaskAlternate,
            "ctrl": kCGEventFlagMaskControl, "control": kCGEventFlagMaskControl,
        }

        key_name = params.get("key", "").lower().strip()
        modifiers = params.get("modifiers", [])

        code = KEY_CODES.get(key_name)
        if code is None:
            btlog(f"press_key: unknown key '{key_name}'")
            return None

        # Build modifier flags
        flags = 0
        for mod in modifiers:
            flag = MODIFIER_FLAGS.get(mod.lower().strip(), 0)
            flags |= flag

        event_down = CGEventCreateKeyboardEvent(None, code, True)
        if flags:
            CGEventSetFlags(event_down, flags)
        CGEventPost(kCGHIDEventTap, event_down)
        time.sleep(0.05)

        event_up = CGEventCreateKeyboardEvent(None, code, False)
        if flags:
            CGEventSetFlags(event_up, flags)
        CGEventPost(kCGHIDEventTap, event_up)

        btlog(f"press_key: {key_name} modifiers={modifiers}")
        return {"success": True}

    # --- scroll: scroll by direction + amount ---
    def handle_scroll(ctx, params):
        from Quartz import (
            CGEventCreateScrollWheelEvent, CGEventPost,
            kCGHIDEventTap, kCGScrollEventUnitLine,
        )

        direction = params.get("direction", "down").lower().strip()
        amount = int(params.get("amount", 3))

        # Scroll amounts: positive = up/left, negative = down/right
        dy, dx = 0, 0
        if direction == "down":
            dy = -amount
        elif direction == "up":
            dy = amount
        elif direction == "left":
            dx = amount
        elif direction == "right":
            dx = -amount
        else:
            btlog(f"scroll: unknown direction '{direction}'")
            return None

        event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx)
        CGEventPost(kCGHIDEventTap, event)

        btlog(f"scroll: {direction} amount={amount}")
        return {"success": True}

    # --- wait_for_element: poll until element appears ---
    def handle_wait_for_element(ctx, params):
        from app.tasks.wait import wait_for_element

        target = params.get("target", "")
        role = params.get("role")
        max_wait = params.get("max_wait", 60.0)

        try:
            element = wait_for_element(ctx.app_name, target, role=role,
                                        max_wait=max_wait)
            return {"success": True, "element": element}
        except RuntimeError:
            return None

    # --- Register all ---
    ctx.register("click", handle_click)
    ctx.register("find_and_click", handle_find_and_click)
    ctx.register("find_and_type", handle_find_and_type)
    ctx.register("find_all", handle_find_all)
    ctx.register("wait", handle_wait)
    ctx.register("extract_question", handle_extract_question)
    ctx.register("discover_menu", handle_discover_menu)
    ctx.register("press_escape", handle_press_escape)
    ctx.register("send_to_llm", handle_send_to_llm)
    ctx.register("lookup_match", handle_lookup_match)
    ctx.register("store_qa", handle_store_qa)
    ctx.register("solve_assessment_page", handle_solve_assessment_page)
    ctx.register("video_poll", handle_video_poll)
    ctx.register("press_key", handle_press_key)
    ctx.register("scroll", handle_scroll)
    ctx.register("wait_for_element", handle_wait_for_element)
