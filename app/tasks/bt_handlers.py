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

    def _activate_ctx_app():
        """Bring ctx.app_name to frontmost. Raw HID handlers (press_key,
        click_at, type_keys, press_escape, scroll) need this because
        CGEventPost(kCGHIDEventTap) targets the frontmost app — if our
        Python window or another app stole focus, our keys go nowhere
        useful. find_and_click already activates via element PID, but raw
        handlers don't have an element to derive PID from.
        """
        from AppKit import (
            NSWorkspace, NSApplicationActivateIgnoringOtherApps,
        )
        target = (ctx.app_name or "").lower()
        if not target:
            return False
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = (app.localizedName() or "").lower()
            if target in name:
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                time.sleep(0.10)
                return True
        btlog(f"_activate_ctx_app: app not found: {ctx.app_name}")
        return False

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
        # Echo raw AX node so server has full visibility on what was clicked
        # (Jesse 2026-05-19: no proactive filtering of interaction targets).
        from app.tasks.serialize_node import serialize_ax_node
        return {"success": True, "ax": serialize_ax_node(element)}

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
        from app.tasks.serialize_node import serialize_ax_node
        return {"success": True, "target": target, "ax": serialize_ax_node(element)}

    # --- find_and_type: find text field + type ---
    def handle_find_and_type(ctx, params):
        from app.tasks.find_element import find_element, find_all_elements
        from app.tasks.click_element import click_element, StaleElementError
        from app.tasks.type_text import type_text

        target = params.get("target", "")
        role = params.get("role")
        text = params.get("text", "")
        focus_strategy = params.get("focus_strategy")
        # Accept pre-resolved element ref (from find_all + for_each).
        # AXSetValue writes to the specific element passed, so plumbing
        # the ref through here lets per-iteration BTs target distinct
        # fields when name/role collide (e.g. table_input "Your answer:").
        element = params.get("element")

        if element is None:
            # When target is empty and searching by role only, use web-area-scoped
            # find_all_elements to avoid hitting browser chrome (e.g., address bar
            # is an AXTextField at depth ~10, before web content at depth ~31).
            if not target and role:
                results = find_all_elements(ctx.app_name, role=role)
                element = results[0][0] if results else None
            else:
                element = find_element(ctx.app_name, target, role=role)
        if not element:
            logger.error(f"find_and_type: field not found (role={role}, element_provided={params.get('element') is not None})")
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
        from app.tasks.serialize_node import serialize_ax_node
        return {"success": True, "ax": serialize_ax_node(element)}

    # --- find_all: find all matching elements, return enriched list ---
    def handle_find_all(ctx, params):
        from app.tasks.find_element import find_all_elements
        from app.tasks.capture_tree import capture_tree
        from app.tasks.serialize_node import serialize_ax_node

        role = params.get("role", "")
        desc_contains = params.get("description_contains")

        results = find_all_elements(ctx.app_name, role=role,
                                     description_contains=desc_contains)

        # Capture tree for label finding (heuristic — preceding AXStaticText).
        tree = capture_tree(ctx.app_name)

        # Dedupe by element identity. find_all_elements walks AXWebArea and
        # can revisit the same element via multiple parent paths (Wonder
        # Blocks wraps elements in many React containers), producing
        # duplicates. Identity of AXUIElement is via underlying CFEqual; we
        # dedupe by id() since same Python wrapper is returned for same element.
        seen_ids = set()
        items = []
        for element, desc in results:
            elem_id = id(element)
            if elem_id in seen_ids:
                continue
            seen_ids.add(elem_id)

            # RAW AX NODE — full attribute set (role, title, description,
            # value, name, position, size, visible_bbox). Per Jesse 2026-05-19:
            # Mac surfaces the unfiltered AX data; server picks what it needs.
            ax = serialize_ax_node(element)

            # CONVENIENCE FIELDS (heuristic, derived):
            #   label       = preceding AXStaticText (Mac-side tree-walk heuristic)
            #   popup_desc  = alias of description (kept for current spark callers)
            #   description = AXDescription as captured by find_all_elements
            #   value       = AXValue (sourced from `ax` for consistency)
            # Server should prefer the `ax` dict for matching identity; the
            # convenience fields are best-effort hints and may be empty.
            label = _find_preceding_label(tree, desc, target_role=role)

            items.append({
                "element": element,
                "ax": ax,
                "description": desc,
                "popup_desc": desc,
                "label": label,
                "value": ax.get("value", ""),
            })

        logger.info(
            f"find_all: {len(items)} unique elements (role={role}; "
            f"raw_results={len(results)})"
        )
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
        from app.tasks.bt_helpers import _extract_menu_nodes

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

        # Return enriched menu items: each is {'text': <display>, 'ax': <raw>}.
        # Per Jesse 2026-05-19 ("Mac is dumb capture, server is smart"): the
        # raw AX node travels alongside the text so server can re-parse any
        # attribute (role/value/bbox/etc.) without a Mac patch. Current
        # callers (lookup_match, select_dropdown_option) read item.text for
        # display-string matching; new server-side consumers can read item.ax.
        items = _extract_menu_nodes(menu_subtree, role)
        logger.info(f"discover_menu: {len(items)} items found")
        return items

    # --- press_escape: dismiss popup ---
    def handle_press_escape(ctx, params):
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap
        )
        from app.tasks.event_routing import ensure_target_frontmost
        _activate_ctx_app()
        # Keyboard event must land in ctx.app. Fail loudly if it's not
        # frontmost (Jesse 2026-06-01 Option A).
        ensure_target_frontmost(ctx.app_name)
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
        from app.tasks import local_kb
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

        # Local KB top-K retrieval (Jesse 2026-05-18): only here, only when a
        # send_to_llm is actually about to fire. Previously fired per /next_action
        # poll tick in pipeline.py, which spammed /api/v1/embed every 5-6s during
        # video polling. Best-effort: any failure logs and continues without it.
        if getattr(ctx, "use_local_kb", True) and question:
            try:
                chunks = local_kb.query(
                    course_id=ctx.course_id, question_text=question, top_k=5
                )
                if chunks:
                    payload["relevant_kb_chunks"] = [c.to_dict() for c in chunks]
                    btlog(
                        f"local_kb retrieval: course={ctx.course_id} "
                        f"{len(chunks)} chunks (top score {chunks[0].score:.3f})"
                    )
            except Exception as e:
                btlog(f"local_kb retrieval failed (continuing): {e}")

        # Always include screenshot so Spark can use vision when needed
        try:
            screenshot_bytes = capture_screenshot(ctx.app_name)
            payload["screenshot_b64"] = base64.b64encode(screenshot_bytes).decode("utf-8")
            btlog(f"send_to_llm: attached screenshot ({len(screenshot_bytes)} bytes)")
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
    # Returns the matched value on hit, the `default` param on miss (default None).
    # When `default` is provided (even as an empty string), the handler returns
    # success and writes the default to blackboard so for_each loops can skip
    # non-matching items via a downstream conditional instead of failing the
    # whole iteration. Without `default`, no-match returns None which fails the
    # action — preserve for callers that rely on that.
    def handle_lookup_match(ctx, params):
        matches = params.get("matches", {})
        key = params.get("key", "")
        has_default = "default" in params
        default = params.get("default")

        btlog(f"lookup_match: key='{key}', matches_keys={list(matches.keys()) if matches else 'None'}")

        if not matches or not key:
            btlog(f"lookup_match: FAIL empty matches={bool(matches)} or key='{key}'")
            return default if has_default else None

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
        return default if has_default else None

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
            # QWERTY US layout (kVK_ANSI_*). Added so press_key supports
            # Cmd+letter shortcuts (select-all, copy, paste, undo, etc.).
            # Gap evidence: yesterday's Khan run consult c_0a40ba7d emitted
            # press_key(key="a", modifiers=["command"]) and was rejected
            # with "press_key: unknown key 'a'".
            "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
            "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
            "y": 16, "t": 17, "u": 32, "i": 34, "o": 31, "p": 35, "l": 37,
            "j": 38, "k": 40, "n": 45, "m": 46,
            "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22,
            "7": 26, "8": 28, "9": 25, "0": 29,
            "/": 44, ".": 47, ",": 43, ";": 41, "\\": 42,
            "-": 27, "=": 24, "[": 33, "]": 30, "\"": 39, "`": 50,
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

        from app.tasks.event_routing import ensure_target_frontmost
        _activate_ctx_app()
        # Keyboard event — must land in ctx.app. Fail loudly if it can't
        # (Jesse 2026-06-01 Option A).
        ensure_target_frontmost(ctx.app_name)
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

    # --- click_at: left-click at point coords ---
    # For Perseus widgets where targets are SVG/canvas not in AX tree
    # (label_image dot regions, interactive_graph plot points). BTs compute
    # coords from find_all results' visible_bbox. Coords are POINTS (logical),
    # same space as visible_bbox — no Retina scale-factor math needed.
    def handle_click_at(ctx, params):
        from Quartz import (
            CGEventCreateMouseEvent,
            kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft,
        )
        from app.tasks.event_routing import post_coord_event_to_app
        try:
            x = float(params["x"])
            y = float(params["y"])
        except (KeyError, TypeError, ValueError) as e:
            btlog(f"click_at: missing/invalid coords: {e}")
            return None
        # Mouse events route via CGEventPostToPid so they land in ctx.app
        # regardless of who is macOS-frontmost (Jesse 2026-06-01 Option B).
        _activate_ctx_app()
        pos = (x, y)
        down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pos, kCGMouseButtonLeft)
        post_coord_event_to_app(down, ctx.app_name)
        time.sleep(0.05)
        up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pos, kCGMouseButtonLeft)
        post_coord_event_to_app(up, ctx.app_name)
        btlog(f"click_at: ({x:.0f},{y:.0f})")
        return {"success": True}

    # --- drag: mouse drag from start → end ---
    # Closes sorter, orderer, matcher, interactive_graph drag interactions.
    # Works for mouse-event-based drag libraries (react-dnd mouse backend,
    # Perseus widgets). HTML5-native drag (dragstart/dragover events) is NOT
    # produced by CGEvents — those widgets would need a different mechanism.
    # Khan widgets observed to use mouse-event-based drag.
    # Timing tuned for React handlers: 80ms press hold lets "drag activate"
    # fire; 15-20 intermediate moves at ~20ms align with rAF batching;
    # 50ms hold before release lets drop targets register hover.
    def handle_drag(ctx, params):
        from Quartz import (
            CGEventCreateMouseEvent,
            kCGEventLeftMouseDown, kCGEventLeftMouseUp,
            kCGEventLeftMouseDragged, kCGMouseButtonLeft,
        )
        from app.tasks.event_routing import post_coord_event_to_app
        start = params.get("start") or {}
        end = params.get("end") or {}
        try:
            sx = float(start["x"]); sy = float(start["y"])
            ex = float(end["x"]); ey = float(end["y"])
        except (KeyError, TypeError, ValueError) as e:
            btlog(f"drag: missing/invalid start/end coords: {e}")
            return None

        steps = int(params.get("steps", 18))
        if steps < 2:
            steps = 2
        step_delay = float(params.get("step_delay", 0.020))
        press_hold = float(params.get("press_hold", 0.080))
        release_hold = float(params.get("release_hold", 0.050))

        # Mouse drag — all events routed via CGEventPostToPid so they land
        # in ctx.app regardless of who is macOS-frontmost (Jesse 2026-06-01
        # Option B).

        # 1. Mouse down at start, hold to let drag handlers activate
        down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (sx, sy), kCGMouseButtonLeft)
        post_coord_event_to_app(down, ctx.app_name)
        time.sleep(press_hold)

        # 2. Intermediate dragged moves
        for i in range(1, steps + 1):
            t = i / steps
            x = sx + (ex - sx) * t
            y = sy + (ey - sy) * t
            move = CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, (x, y), kCGMouseButtonLeft)
            post_coord_event_to_app(move, ctx.app_name)
            time.sleep(step_delay)

        # 3. Hold at end so drop targets register hover, then release
        time.sleep(release_hold)
        up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (ex, ey), kCGMouseButtonLeft)
        post_coord_event_to_app(up, ctx.app_name)
        btlog(f"drag: ({sx:.0f},{sy:.0f}) -> ({ex:.0f},{ey:.0f}) steps={steps}")
        return {"success": True}

    # --- type_keys: type arbitrary Unicode text into the focused element ---
    # Uses CGEventKeyboardSetUnicodeString so any codepoint works without a
    # keymap — math symbols (× ÷ ° π), Greek (α β γ), subscripts (H₂O), accents.
    # Falls back path: caller must focus the target element first (click).
    def handle_type_keys(ctx, params):
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
            CGEventPost, kCGHIDEventTap,
        )
        from app.tasks.event_routing import ensure_target_frontmost
        text = params.get("text", "")
        if not text:
            return {"success": True}
        per_char_delay = float(params.get("per_char_delay", 0.010))
        _activate_ctx_app()
        # Keyboard events — must land in ctx.app (Jesse 2026-06-01 Option A).
        ensure_target_frontmost(ctx.app_name)
        for ch in text:
            e_down = CGEventCreateKeyboardEvent(None, 0, True)
            CGEventKeyboardSetUnicodeString(e_down, 1, ch)
            CGEventPost(kCGHIDEventTap, e_down)
            e_up = CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(e_up, 1, ch)
            CGEventPost(kCGHIDEventTap, e_up)
            time.sleep(per_char_delay)
        btlog(f"type_keys: {len(text)} chars")
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

        # Scroll routes via CGEventPostToPid (coord-class event, Jesse
        # 2026-06-01 Option B) so it lands in ctx.app even when not frontmost.
        from app.tasks.event_routing import post_coord_event_to_app
        event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx)
        post_coord_event_to_app(event, ctx.app_name)

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

    # --- select_dropdown_option: semantic ARIA combobox/listbox selection ---
    # Owns the full local strategy ladder for ARIA listboxes. Spark tells us
    # WHICH option to pick; we own HOW. Strategy ladder: focus_press
    # (VoiceOver path — drives real DOM focus then AXPress) → focus_space
    # → focus_enter → mouse_click → ax_press. After each attempt, verifies
    # the trigger combobox AXValue contains the chosen option text. Only
    # returns success when state change is observed.
    #
    # Walks the FULL app AX tree, NOT scoped to AXWebArea — Wonder Blocks
    # portals options OUTSIDE the web area via ReactDOM.createPortal.
    # Excludes the macOS menu bar branch to avoid matching system menus.
    def handle_select_dropdown_option(ctx, params):
        from AppKit import (
            NSWorkspace, NSApplicationActivateIgnoringOtherApps,
        )
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementSetAttributeValue,
            AXUIElementPerformAction,
            kAXErrorSuccess,
            kAXChildrenAttribute,
            kAXRoleAttribute,
            kAXTitleAttribute,
            kAXDescriptionAttribute,
            kAXValueAttribute,
            kAXFocusedAttribute,
            kAXPressAction,
        )
        from CoreFoundation import (
            CFArrayGetCount, CFArrayGetValueAtIndex,
        )
        from app.tasks.find_element import find_element
        from app.tasks.click_element import click_element, StaleElementError

        option_text = (
            params.get("option") or params.get("option_text") or params.get("target")
        )
        if not option_text:
            btlog("select_dropdown_option: missing 'option' param")
            return {"success": False, "error": "missing option"}

        trigger = params.get("trigger_element") or params.get("element")
        trigger_target = (
            params.get("trigger_target")
            or params.get("combobox")
            or "Select an answer"
        )
        trigger_role = params.get("trigger_role", "AXComboBox")
        trigger_match_mode = params.get("trigger_match_mode", "contains")
        open_strategy = params.get("open_strategy", "mouse_click")
        open_wait = float(params.get("open_wait", 0.5))
        verify_wait = float(params.get("verify_wait", 0.35))
        strategies = params.get("strategies") or [
            "focus_press", "focus_space", "focus_enter", "mouse_click", "ax_press",
        ]

        # If trigger is a dict from find_all, unwrap.
        if isinstance(trigger, dict):
            trigger = trigger.get("element")

        def norm(s):
            s = str(s or "").strip()
            for suffix in (" not selected", " selected"):
                if s.lower().endswith(suffix):
                    s = s[: -len(suffix)].strip()
            return s.lower()

        wanted = norm(option_text)

        def attr(el, ax_attr):
            err, val = AXUIElementCopyAttributeValue(el, ax_attr, None)
            if err == kAXErrorSuccess and val is not None:
                return str(val)
            return ""

        def _menu_item_alive():
            """Probe the menu_item AX element. Returns False when the menu
            has closed (element invalidated, role attribute unreadable).

            Used as the primary verify signal: when a strategy clicks the
            menu_item and the menu CLOSES in response, the element ref
            becomes invalid. That close is the canonical commit signal —
            independent of whether the trigger's AXValue reflects the
            selected option text.
            """
            try:
                err, _ = AXUIElementCopyAttributeValue(
                    menu_item, kAXRoleAttribute, None,
                )
                return err == kAXErrorSuccess
            except Exception:
                return False

        def verify_selected():
            # PRIMARY signal: did the menu close? Khan's Wonder Blocks
            # SingleSelect (used by EXERCISE_DROPDOWN_MATCHING and other
            # match-row widgets) puts the row PROMPT text in AXComboBox.value,
            # never the selected option. AXValue-comparison will FAIL even
            # when selection commits successfully (taey-ed defect
            # 2026-05-19 23:36 — option='less' chosen, menu closed, but
            # value='the ball moved down and to the right...' so all 5
            # strategies "failed" verification while the form was actually
            # in the correct state). Menu-closed is the platform-agnostic
            # commit signal — it works on both traditional AXPopUpButton
            # widgets and React-based combobox+listbox patterns.
            time.sleep(verify_wait)
            if not _menu_item_alive():
                btlog(
                    "select_dropdown_option: verify menu_closed=True "
                    f"(commit signal; wanted={wanted!r})"
                )
                return True

            # SECONDARY signal: trigger's AXValue matches the wanted option.
            # Useful for traditional widgets where the value field reflects
            # the selected option after commit. Substring rather than equality
            # to handle placeholders, but with the false-positive risk on
            # comboboxes whose placeholder substring-contains the option
            # (e.g. placeholder '+/-' contains '+'). Two-pass exact-first
            # match-finding upstream mitigates that for the menu_item picker;
            # here we just accept any of the three text attrs containing
            # `wanted`.
            value = norm(attr(trigger, kAXValueAttribute))
            title = norm(attr(trigger, kAXTitleAttribute))
            desc = norm(attr(trigger, kAXDescriptionAttribute))
            if not wanted:
                return False
            ok = wanted in (value, title, desc)
            btlog(
                f"select_dropdown_option: verify value={value!r} "
                f"wanted={wanted!r} ok={ok}"
            )
            return ok

        # Activate browser app before any synthesis.
        _activate_ctx_app()

        # Resolve trigger if not pre-supplied.
        if trigger is None:
            trigger = find_element(
                ctx.app_name, trigger_target,
                role=trigger_role, match_mode=trigger_match_mode,
            )
        if trigger is None and trigger_role != "AXPopUpButton":
            # Some Khan widgets use native popup. Fall back.
            trigger = find_element(
                ctx.app_name, trigger_target,
                role="AXPopUpButton", match_mode=trigger_match_mode,
            )
        if trigger is None:
            btlog(
                f"select_dropdown_option: trigger not found "
                f"target={trigger_target!r} role={trigger_role}"
            )
            return {"success": False, "error": "trigger not found"}

        # Open the dropdown.
        try:
            click_element(trigger, strategy=open_strategy)
        except Exception as e:
            btlog(f"select_dropdown_option: open failed: {e}")
            return {"success": False, "error": f"open failed: {e}"}
        time.sleep(open_wait)

        # Build full-app root for menu-item discovery.
        target_app = None
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = (app.localizedName() or "").lower()
            if (ctx.app_name or "").lower() in name:
                target_app = app
                break
        if target_app is None:
            return {
                "success": False,
                "error": f"app not found: {ctx.app_name}",
            }
        root = AXUIElementCreateApplication(target_app.processIdentifier())

        def iter_ax(el, under_menu_bar=False):
            role = attr(el, kAXRoleAttribute)
            now_under = under_menu_bar or role == "AXMenuBar"
            if not now_under:
                yield el
            if role == "AXMenuBar":
                return
            err, children = AXUIElementCopyAttributeValue(
                el, kAXChildrenAttribute, None,
            )
            if err == kAXErrorSuccess and children:
                count = CFArrayGetCount(children)
                for i in range(count):
                    yield from iter_ax(
                        CFArrayGetValueAtIndex(children, i), now_under,
                    )

        def menu_texts(el):
            return [
                v for v in (
                    attr(el, kAXTitleAttribute),
                    attr(el, kAXDescriptionAttribute),
                    attr(el, kAXValueAttribute),
                ) if v
            ]

        # Two-pass match: prefer exact match over substring. Without this,
        # tree-walk order picks placeholder "+/-" before the real "+" item
        # because '+' is a substring of '+/-' (caught during Khan Apply: Ions
        # ion-charge sign dropdown — option='+' was matching '+/-' placeholder
        # then false-positive verifying because '+' is in '+/-').
        menu_item = None
        seen = []
        candidates = []
        for el in iter_ax(root):
            if attr(el, kAXRoleAttribute) != "AXMenuItem":
                continue
            texts = menu_texts(el)
            display = texts[0] if texts else ""
            seen.append(display)
            normalized = [norm(t) for t in texts]
            candidates.append((el, normalized))

        # Pass 1: exact match.
        if wanted:
            for el, normalized in candidates:
                if any(wanted == t for t in normalized):
                    menu_item = el
                    break
        # Pass 2: substring match (fallback for descriptive option text).
        if menu_item is None and wanted:
            for el, normalized in candidates:
                if any(wanted in t for t in normalized):
                    menu_item = el
                    break

        btlog(f"select_dropdown_option: menu_items_seen={seen[:12]}")
        if menu_item is None:
            return {
                "success": False,
                "error": f"option not found: {option_text}",
                "seen": seen[:20],
            }

        # Strategy ladder. Stop only on observed state change.
        errors = []
        for strategy in strategies:
            try:
                btlog(
                    f"select_dropdown_option: trying {strategy} "
                    f"option={option_text!r}"
                )
                if strategy == "focus_press":
                    err = AXUIElementSetAttributeValue(
                        menu_item, kAXFocusedAttribute, True,
                    )
                    if err != kAXErrorSuccess:
                        raise RuntimeError(f"AX focus failed: {err}")
                    time.sleep(0.20)
                    err = AXUIElementPerformAction(menu_item, kAXPressAction)
                    if err != kAXErrorSuccess:
                        raise RuntimeError(f"AXPress failed: {err}")
                else:
                    click_element(menu_item, strategy=strategy)

                if verify_selected():
                    return {
                        "success": True,
                        "option": option_text,
                        "strategy": strategy,
                    }
            except StaleElementError as e:
                errors.append(f"{strategy}: stale {e}")
            except Exception as e:
                errors.append(f"{strategy}: {e}")

        return {
            "success": False,
            "error": "no strategy produced verified selection",
            "option": option_text,
            "errors": errors,
            "seen": seen[:20],
        }

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
    ctx.register("click_at", handle_click_at)
    ctx.register("drag", handle_drag)
    ctx.register("type_keys", handle_type_keys)
    ctx.register("select_dropdown_option", handle_select_dropdown_option)
    ctx.register("click_element", handle_click)  # alias for legacy/consultation BTs
