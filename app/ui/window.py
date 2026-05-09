"""
Taey-Ed - Main Window

Uses pipeline.py to run automation.
All tests go through this UI.

Phase 8 UX Features (Feb 2026):
- Screen count limit (spinner, 0=unlimited)
- Progress display (X/Y screens completed)
- "Stop After This Screen" button
- Global hotkey kill switch (Cmd+Shift+Escape)
- Menu bar status icon with Stop control

Phase 9: Persistent Chat Window (Feb 2026):
- Chat panel replaces Report Issue panel and modal dialog
- System status messages, questions, and user responses
- History loaded from Spark Redis on startup
- Pipeline thread blocks on user input via threading.Event
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue
import logging
import subprocess
import sys
import time
import datetime


# Platform registry: each entry defines how to find the app in macOS accessibility.
# - "app_name": The process name macOS uses (what shows in Activity Monitor)
# - "type": "app" for native Mac apps, "browser" for websites
# - "browser": Which browser to target (for type="browser" only)
# - "label": Display name in the UI dropdown
PLATFORMS = {
    "acellus": {
        "app_name": "Acellus",
        "type": "app",
        "label": "Acellus",
    },
    "coursera": {
        "app_name": "Google Chrome",
        "type": "browser",
        "browser": "Google Chrome",
        "label": "Coursera (Chrome)",
    },
    "khan_academy": {
        "app_name": "Google Chrome",
        "type": "browser",
        "browser": "Google Chrome",
        "label": "Khan Academy (Chrome)",
    },
    "edx": {
        "app_name": "Google Chrome",
        "type": "browser",
        "browser": "Google Chrome",
        "label": "edX (Chrome)",
    },
    "udemy": {
        "app_name": "Google Chrome",
        "type": "browser",
        "browser": "Google Chrome",
        "label": "Udemy (Chrome)",
    },
}

# Backwards compat
PLATFORM_APP_NAMES = {k: v["app_name"] for k, v in PLATFORMS.items()}


def check_accessibility_permission() -> bool:
    """Check if app has accessibility permission."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        return AXIsProcessTrusted()
    except ImportError:
        # If we can't import, assume we're okay (non-Mac or dev environment)
        return True


def prompt_for_accessibility():
    """Show dialog and open System Settings for accessibility."""
    msg = (
        "Taey-Ed needs Accessibility permission to automate applications.\n\n"
        "Click OK to open System Settings.\n"
        "Then add Taey-Ed.app to the Accessibility list and restart the app."
    )
    messagebox.showwarning("Accessibility Permission Required", msg)

    # Open System Settings to Accessibility pane
    subprocess.run([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ])
    sys.exit(1)


class QueueHandler(logging.Handler):
    """Thread-safe logging handler that queues messages for UI display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        msg = self.format(record)
        self.log_queue.put(msg)


class TaeyEdWindow:
    """Main application window for Taey-Ed."""

    def __init__(self):
        # Check permissions FIRST before building UI
        if not check_accessibility_permission():
            # Need a temporary root for the dialog
            temp_root = tk.Tk()
            temp_root.withdraw()
            prompt_for_accessibility()

        self.root = tk.Tk()
        self.root.title("Taey-Ed")
        self.root.geometry("700x700")

        # Message queue for thread-safe logging
        self.log_queue = queue.Queue()

        # Stop event for continuous mode
        self.stop_event = threading.Event()

        # Track running state for menu bar and hotkey
        self._is_running = False
        self._screens_completed = 0

        # Chat state
        self._chat_queue = queue.Queue()         # System messages → UI
        self._pending_user_messages = queue.Queue()  # User messages → pipeline
        self._chat_event = threading.Event()     # Pipeline blocks on this
        self._chat_event.set()                   # Start in "not waiting" state
        self._chat_response = {"text": ""}       # Shared response text

        # Setup logging
        self._setup_logging()

        # Build UI
        self._build_ui()

        # Setup global hotkey (Cmd+Shift+Escape)
        self._setup_global_hotkey()

        # Setup menu bar status icon
        self._setup_menu_bar()

        # Wire ALL exit paths to graceful shutdown. Without this, exit
        # paths bypass _on_close → pipeline keeps polling, consultation
        # stays pending, lockfile may persist.
        # - WM_DELETE_WINDOW: red X close button
        # - ::tk::mac::Quit: Cmd+Q + Apple Event "Quit" (osascript, dock menu)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.root.createcommand("::tk::mac::Quit", self._on_close)
        except Exception as e:
            self.logger.warning(f"Could not register tk::mac::Quit handler: {e}")

        # Start queue processors
        self.root.after(100, self._process_log_queue)
        self.root.after(100, self._process_chat_queue)

        # Load chat history on startup
        self.root.after(500, self._load_chat_history)

    def _setup_logging(self):
        """Configure logging to use queue handler."""
        self.logger = logging.getLogger("taey-ed")
        self.logger.setLevel(logging.INFO)

        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)

    def _build_ui(self):
        """Build the UI components."""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(main_frame, text="Taey-Ed", font=("Helvetica", 16, "bold"))
        title_label.pack(pady=(0, 10))

        # Status label
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(main_frame, textvariable=self.status_var)
        status_label.pack(pady=(0, 10))

        # === Platform Selection Frame ===
        platform_frame = ttk.LabelFrame(main_frame, text="Platform", padding="5")
        platform_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(platform_frame, text="Select Platform:").pack(side=tk.LEFT, padx=(0, 10))

        # Build display labels → platform key mapping
        self._label_to_key = {v["label"]: k for k, v in PLATFORMS.items()}
        platform_labels = list(self._label_to_key.keys())

        # Default to khan_academy (current production target). Falls back
        # to first available label if the key has been removed.
        _default_label = PLATFORMS.get("khan_academy", {}).get("label") or platform_labels[0]
        self.platform_var = tk.StringVar(value=_default_label)
        self.platform_combo = ttk.Combobox(
            platform_frame,
            textvariable=self.platform_var,
            values=platform_labels,
            state="readonly",
            width=25
        )
        self.platform_combo.pack(side=tk.LEFT)

        # Platform type indicator
        self.platform_type_var = tk.StringVar(value="")
        self.platform_type_label = ttk.Label(
            platform_frame, textvariable=self.platform_type_var,
            font=("Helvetica", 10)
        )
        self.platform_type_label.pack(side=tk.LEFT, padx=(10, 0))
        self.platform_combo.bind("<<ComboboxSelected>>", self._on_platform_changed)
        self._on_platform_changed(None)  # set initial

        # === Course ID Frame ===
        course_frame = ttk.LabelFrame(main_frame, text="Course", padding="5")
        course_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(course_frame, text="Course ID:").pack(side=tk.LEFT, padx=(0, 10))
        self.course_id_var = tk.StringVar(value="unknown")
        self.course_id_entry = ttk.Entry(
            course_frame, textvariable=self.course_id_var, width=30
        )
        self.course_id_entry.pack(side=tk.LEFT)
        ttk.Label(
            course_frame, text="(e.g., intro_banking, cs101)",
            font=("Helvetica", 9)
        ).pack(side=tk.LEFT, padx=(10, 0))

        # === Screen Count Frame ===
        screen_frame = ttk.LabelFrame(main_frame, text="Screen Limit", padding="5")
        screen_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(screen_frame, text="Max Screens:").pack(side=tk.LEFT, padx=(0, 5))
        self.max_screens_var = tk.IntVar(value=0)
        self.max_screens_spin = ttk.Spinbox(
            screen_frame,
            from_=0,
            to=999,
            textvariable=self.max_screens_var,
            width=5,
        )
        self.max_screens_spin.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            screen_frame, text="(0 = unlimited)",
            font=("Helvetica", 9)
        ).pack(side=tk.LEFT, padx=(0, 20))

        # Progress display (visible during continuous mode)
        self.progress_var = tk.StringVar(value="")
        self.progress_label = ttk.Label(
            screen_frame, textvariable=self.progress_var,
            font=("Helvetica", 11, "bold")
        )
        self.progress_label.pack(side=tk.LEFT, padx=(10, 0))

        # === Action Buttons Frame ===
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=10)

        # Run One Screen button
        self.run_button = ttk.Button(
            button_frame,
            text="Run One Screen",
            command=self._on_run_one_screen
        )
        self.run_button.pack(side=tk.LEFT, padx=(0, 10))

        # Run Continuous button
        self.continuous_button = ttk.Button(
            button_frame,
            text="Run Continuous",
            command=self._on_run_continuous
        )
        self.continuous_button.pack(side=tk.LEFT, padx=(0, 10))

        # Stop button (disabled until continuous mode is running)
        self.stop_button = ttk.Button(
            button_frame,
            text="Stop",
            command=self._on_stop,
            state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 10))

        # Stop After This Screen button (disabled until continuous mode is running)
        self.stop_after_button = ttk.Button(
            button_frame,
            text="Stop After This Screen",
            command=self._on_stop_after_this_screen,
            state=tk.DISABLED
        )
        self.stop_after_button.pack(side=tk.LEFT, padx=(0, 10))

        # === Result Frame ===
        result_frame = ttk.LabelFrame(main_frame, text="Result", padding="5")
        result_frame.pack(fill=tk.X, pady=(0, 10))

        self.result_var = tk.StringVar(value="Ready to run")
        result_label = ttk.Label(
            result_frame,
            textvariable=self.result_var,
            font=("Helvetica", 12),
            wraplength=650
        )
        result_label.pack(fill=tk.X)

        # === Log Display ===
        log_label = ttk.Label(main_frame, text="Log:")
        log_label.pack(anchor=tk.W)

        self.log_text = scrolledtext.ScrolledText(
            main_frame,
            height=6,
            width=80,
            state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        # === Chat Panel ===
        chat_frame = ttk.LabelFrame(main_frame, text="Chat", padding="5")
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        # Chat display (read-only)
        self.chat_display = scrolledtext.ScrolledText(
            chat_frame,
            height=8,
            width=80,
            state=tk.DISABLED,
            wrap=tk.WORD,
            font=("Helvetica", 11),
        )
        self.chat_display.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # Configure text tags for styling
        self.chat_display.tag_configure("system", foreground="#666666")
        self.chat_display.tag_configure("user", foreground="#0066cc")
        self.chat_display.tag_configure("question", foreground="#cc6600", font=("Helvetica", 11, "bold"))
        self.chat_display.tag_configure("error", foreground="#cc0000")
        self.chat_display.tag_configure("timestamp", foreground="#999999", font=("Helvetica", 9))

        # Chat input row
        input_frame = ttk.Frame(chat_frame)
        input_frame.pack(fill=tk.X)

        self.chat_entry = ttk.Entry(input_frame, font=("Helvetica", 11))
        self.chat_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.chat_entry.bind("<Return>", lambda e: self._on_send_chat())

        self.chat_send_button = ttk.Button(
            input_frame,
            text="Send",
            command=self._on_send_chat,
        )
        self.chat_send_button.pack(side=tk.RIGHT)

    # =========================================================================
    # Chat Panel Logic
    # =========================================================================

    def _append_chat_message(self, sender: str, text: str, msg_type: str = "status", timestamp: float = None):
        """Append a message to the chat display (must be called from main thread)."""
        self.chat_display.config(state=tk.NORMAL)

        # Timestamp
        ts = timestamp or time.time()
        ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

        # Determine tag
        if sender == "user":
            tag = "user"
            prefix = "You"
        elif msg_type == "question":
            tag = "question"
            prefix = "System"
        elif msg_type == "error":
            tag = "error"
            prefix = "System"
        else:
            tag = "system"
            prefix = "System"

        self.chat_display.insert(tk.END, f"[{ts_str}] ", "timestamp")
        self.chat_display.insert(tk.END, f"{prefix}: ", tag)
        self.chat_display.insert(tk.END, f"{text}\n", tag)
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _process_chat_queue(self):
        """Process queued chat messages (runs on main thread)."""
        while not self._chat_queue.empty():
            try:
                msg = self._chat_queue.get_nowait()
                self._append_chat_message(
                    sender=msg.get("sender", "system"),
                    text=msg.get("text", ""),
                    msg_type=msg.get("msg_type", "status"),
                    timestamp=msg.get("timestamp"),
                )
                # Flash the chat frame when a question arrives
                if msg.get("msg_type") == "question":
                    self._flash_chat_attention()
            except queue.Empty:
                break
        self.root.after(100, self._process_chat_queue)

    def _flash_chat_attention(self):
        """Briefly highlight the chat frame to draw attention."""
        self.chat_display.config(background="#fff3cd")  # Light yellow
        self.root.after(2000, lambda: self.chat_display.config(background="white"))

    def _on_send_chat(self):
        """Handle Send button / Enter key in chat entry."""
        text = self.chat_entry.get().strip()
        if not text:
            return

        self.chat_entry.delete(0, tk.END)

        # Display immediately in chat
        self._append_chat_message("user", text, "answer")

        if not self._chat_event.is_set():
            # Pipeline is WAITING for user input — unblock it
            self._chat_response["text"] = text
            self._chat_event.set()
            self.logger.info(f"Chat: user response sent to pipeline: {text[:80]}")
        else:
            # Normal operation — queue as proactive message for next /next_action
            self._pending_user_messages.put(text)
            self.logger.info(f"Chat: queued proactive message: {text[:80]}")

    def deliver_chat_messages(self, messages: list):
        """Called from pipeline thread to deliver system messages to chat.

        Thread-safe: puts messages on queue for main thread processing.
        """
        for msg in messages:
            self._chat_queue.put(msg)

    def wait_for_user_input(self, directive: dict) -> str:
        """Called from pipeline thread when user_input_needed.

        Delivers chat messages (which include the question), then blocks
        until user responds in chat panel.

        Returns user's response text, or "" if timeout/cancelled.
        """
        # Deliver any chat messages from the directive
        chat_messages = directive.get("chat_messages", [])
        for msg in chat_messages:
            self._chat_queue.put(msg)

        # If no chat messages, create a default question
        if not chat_messages:
            reason = directive.get("reason", "Help needed")
            self._chat_queue.put({
                "sender": "system",
                "text": reason,
                "msg_type": "question",
                "timestamp": time.time(),
            })

        # Block pipeline thread until user responds
        self._chat_event.clear()  # Reset event
        self._chat_response["text"] = ""

        # Wait up to 10 minutes for user response
        got_response = self._chat_event.wait(timeout=600.0)

        if got_response and self._chat_response["text"]:
            return self._chat_response["text"]
        return ""

    def get_pending_chat_message(self) -> str:
        """Get one pending proactive user message, or None."""
        try:
            return self._pending_user_messages.get_nowait()
        except queue.Empty:
            return None

    def _load_chat_history(self):
        """Load chat history from Spark on startup."""
        platform, _ = self._get_selected_platform()
        if not platform:
            return

        def _load():
            try:
                from app.tasks.call_spark import call_spark
                result = call_spark(f"/chat/{platform}/history", method="GET")
                messages = result.get("messages", [])
                if messages:
                    for msg in messages[-20:]:  # Last 20 messages
                        self._chat_queue.put(msg)
                    self.logger.info(f"Loaded {len(messages)} chat history messages")
            except Exception as e:
                self.logger.warning(f"Could not load chat history: {e}")

        threading.Thread(target=_load, daemon=True).start()

    # =========================================================================
    # Queue + UI Processing
    # =========================================================================

    def _process_log_queue(self):
        """Process queued log messages (runs on main thread)."""
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
            except queue.Empty:
                pass
        self.root.after(100, self._process_log_queue)

    def _on_platform_changed(self, event):
        """Update type indicator when platform selection changes."""
        label = self.platform_var.get()
        key = self._label_to_key.get(label, "")
        pconfig = PLATFORMS.get(key, {})
        ptype = pconfig.get("type", "")
        if ptype == "browser":
            self.platform_type_var.set(f"[Browser: {pconfig.get('browser', '?')}]")
        else:
            self.platform_type_var.set("[Native App]")

    def _get_selected_platform(self):
        """Resolve the selected dropdown label to (platform_key, app_name)."""
        label = self.platform_var.get()
        key = self._label_to_key.get(label)
        if not key:
            return None, None
        return key, PLATFORMS[key]["app_name"]

    def _on_run_one_screen(self):
        """Handle Run One Screen button click."""
        self.run_button.config(state=tk.DISABLED)
        self.status_var.set("Running...")
        self.result_var.set("Running pipeline...")

        # Run in background thread
        thread = threading.Thread(target=self._run_one_screen_worker, daemon=True)
        thread.start()

    def _run_one_screen_worker(self):
        """Background worker for running one screen cycle."""
        platform, app_name = self._get_selected_platform()
        course_id = self.course_id_var.get().strip() or "unknown"
        platform_type = PLATFORMS.get(platform, {}).get("type", "app")

        if not app_name:
            self._show_error(f"Unknown platform: {self.platform_var.get()}")
            return

        self.logger.info(f"=== Run One Screen: {platform} ({app_name}) type={platform_type} course={course_id} ===")

        try:
            from app.pipeline import run_one_screen

            self.logger.info("Calling pipeline.run_one_screen()...")
            result = run_one_screen(platform, app_name, course_id=course_id, platform_type=platform_type)
            self.logger.info(f"Result: {result}")

            if result.get("success"):
                screen = result.get("screen", "unknown")
                action = result.get("action", "unknown")
                result_text = f"SUCCESS: {screen}\nAction: {action}"
                self._show_result(result_text)
                self._update_status(f"Success: {screen}")
            else:
                reason = result.get("reason", "unknown")
                if reason == "no_match":
                    result_text = "NO MATCH - Screen not recognized\nNeeds consultation"
                elif reason == "element_not_found":
                    target = result.get("target", "unknown")
                    result_text = f"ELEMENT NOT FOUND: {target}"
                else:
                    result_text = f"FAILED: {reason}"
                self._show_result(result_text)
                self._update_status(f"Failed: {reason}")

            self.logger.info("=== Complete ===")

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            self.logger.error(f"CRASH: {e}\n{error_detail}")
            self._show_error(f"Pipeline crashed: {e}")

        finally:
            self.root.after(0, lambda: self.run_button.config(state=tk.NORMAL))

    def _on_stop_after_this_screen(self):
        """Stop after the current screen finishes."""
        self.logger.info("Stop after this screen requested")
        self.stop_event.set()
        self.stop_after_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.DISABLED)
        self._update_status("Finishing current screen...")
        self._update_menu_bar_title("T: finishing...")

    def _on_run_continuous(self):
        """Handle Run Continuous button click."""
        self.run_button.config(state=tk.DISABLED)
        self.continuous_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.stop_after_button.config(state=tk.NORMAL)
        self.stop_event.clear()
        self._is_running = True
        self._screens_completed = 0

        max_screens = self.max_screens_var.get()
        if max_screens > 0:
            self.progress_var.set(f"0 / {max_screens}")
            self.status_var.set(f"Running (0/{max_screens})...")
        else:
            self.progress_var.set("0 screens")
            self.status_var.set("Running continuous...")
        self.result_var.set("Continuous mode running...")
        self._update_menu_bar_title("T: running")

        thread = threading.Thread(target=self._run_continuous_worker, daemon=True)
        thread.start()

    def _on_stop(self):
        """Handle Stop button click."""
        self.logger.info("Stop requested by user")
        self.stop_event.set()
        # Also unblock chat wait if pipeline is waiting for user input
        if not self._chat_event.is_set():
            self._chat_event.set()
        self.stop_button.config(state=tk.DISABLED)
        self.stop_after_button.config(state=tk.DISABLED)
        self.status_var.set("Stopping...")
        self._update_menu_bar_title("T: stopping")

    def _screen_callback(self, screens_completed, max_screens):
        """Called by pipeline after each screen completes. Updates UI progress."""
        self._screens_completed = screens_completed
        if max_screens > 0:
            text = f"{screens_completed} / {max_screens}"
            status = f"Running ({screens_completed}/{max_screens})..."
        else:
            text = f"{screens_completed} screens"
            status = f"Running ({screens_completed} screens)..."
        self.root.after(0, lambda: self.progress_var.set(text))
        self.root.after(0, lambda: self.status_var.set(status))
        # Update menu bar
        if max_screens > 0:
            self._update_menu_bar_title(f"T: {screens_completed}/{max_screens}")
        else:
            self._update_menu_bar_title(f"T: {screens_completed}")

    def _run_continuous_worker(self):
        """Background worker for continuous mode."""
        platform, app_name = self._get_selected_platform()
        course_id = self.course_id_var.get().strip() or "unknown"
        platform_type = PLATFORMS.get(platform, {}).get("type", "app")
        max_screens = self.max_screens_var.get()

        if not app_name:
            self._show_error(f"Unknown platform: {self.platform_var.get()}")
            self._reset_continuous_buttons()
            return

        self.logger.info(
            f"=== Continuous Mode: {platform} ({app_name}) type={platform_type} "
            f"course={course_id} max_screens={max_screens} ==="
        )

        try:
            from app.pipeline import run_continuous

            result = run_continuous(
                platform, app_name,
                stop_event=self.stop_event,
                course_id=course_id,
                platform_type=platform_type,
                max_screens=max_screens,
                screen_callback=self._screen_callback,
                chat_message_callback=self.deliver_chat_messages,
                user_input_callback=self.wait_for_user_input,
                pending_chat_messages=self,
            )
            self.logger.info(f"Continuous result: {result}")

            screens = result.get("screens_completed", 0)
            reason = result.get("reason", "unknown")

            if reason == "max_screens_reached":
                result_text = f"Completed {screens} screens (limit reached)"
                self._update_status(f"Done ({screens}/{max_screens} screens)")
            elif reason == "stopped_by_user":
                result_text = f"Stopped by user. Screens completed: {screens}"
                self._update_status(f"Stopped ({screens} screens)")
            elif reason == "safety_halt":
                result_text = f"SAFETY HALT: {result.get('detected', '?')}. Screens: {screens}"
                self._update_status("Safety halt!")
            elif result.get("success"):
                result_text = f"Completed. Screens: {screens}"
                self._update_status(f"Done ({screens} screens)")
            else:
                result_text = f"Stopped: {reason}. Screens completed: {screens}"
                self._update_status(f"Failed: {reason}")

            self._show_result(result_text)

        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            self.logger.error(f"CRASH: {e}\n{error_detail}")
            self._show_error(f"Continuous mode crashed: {e}")

        finally:
            self._is_running = False
            self._reset_continuous_buttons()
            self._update_menu_bar_title("T: idle")

    def _reset_continuous_buttons(self):
        """Re-enable buttons after continuous mode ends. Clear stop_event so restart works."""
        self.stop_event.clear()
        self.root.after(0, lambda: self.run_button.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.continuous_button.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.stop_button.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.stop_after_button.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.status_var.set("Ready"))

    def _show_result(self, text: str):
        """Thread-safe result update."""
        self.root.after(0, lambda: self.result_var.set(text))

    def _show_error(self, error_msg: str):
        """Show error in result and log, then re-enable button."""
        self.logger.error(error_msg)
        self._show_result(f"ERROR: {error_msg}")
        self._update_status("Error occurred")
        self.root.after(0, lambda: self.run_button.config(state=tk.NORMAL))

    def _update_status(self, msg: str):
        """Thread-safe status update."""
        self.root.after(0, lambda: self.status_var.set(msg))

    # =========================================================================
    # Global Hotkey (Cmd+Shift+Escape)
    # =========================================================================

    def _setup_global_hotkey(self):
        """Register Cmd+Shift+Escape as global kill switch using AppKit."""
        try:
            from AppKit import NSEvent, NSKeyDownMask, NSCommandKeyMask, NSShiftKeyMask
            import Quartz

            def hotkey_handler(event):
                # Check for Cmd+Shift+Escape (keyCode 53 = Escape)
                flags = event.modifierFlags()
                key_code = event.keyCode()
                if key_code == 53 and (flags & NSCommandKeyMask) and (flags & NSShiftKeyMask):
                    self.logger.info("GLOBAL HOTKEY: Cmd+Shift+Escape — emergency stop")
                    self.root.after(0, self._emergency_stop)

            self._hotkey_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSKeyDownMask, hotkey_handler
            )
            self.logger.info("Global hotkey registered: Cmd+Shift+Escape")
        except Exception as e:
            # Non-fatal: hotkey is a convenience, not a requirement
            self.logger.warning(f"Could not register global hotkey: {e}")
            self._hotkey_monitor = None

    def _emergency_stop(self):
        """Emergency stop triggered by global hotkey."""
        if self._is_running:
            self.logger.info("EMERGENCY STOP — killing automation")
            self.stop_event.set()
            self._on_stop()
        else:
            self.logger.info("Emergency stop pressed but nothing running")

    # =========================================================================
    # Menu Bar Status Icon
    # =========================================================================

    def _setup_menu_bar(self):
        """Create a macOS menu bar status item showing Taey-Ed state."""
        try:
            from AppKit import (
                NSStatusBar, NSVariableStatusItemLength, NSMenuItem, NSMenu,
                NSObject, NSApplication,
            )
            import objc

            # Create a delegate class to handle menu actions
            class MenuDelegate(NSObject):
                window_ref = None

                @objc.python_method
                def set_window(self, window):
                    self.window_ref = window

                def stopFromMenu_(self, sender):
                    if self.window_ref:
                        self.window_ref.root.after(0, self.window_ref._on_stop)

                def showFromMenu_(self, sender):
                    if self.window_ref:
                        self.window_ref.root.after(0, self.window_ref._bring_to_front)

                def quitFromMenu_(self, sender):
                    if self.window_ref:
                        self.window_ref.root.after(0, self.window_ref.root.destroy)
                    else:
                        NSApplication.sharedApplication().terminate_(None)

            self._menu_delegate = MenuDelegate.alloc().init()
            self._menu_delegate.set_window(self)

            self._status_bar = NSStatusBar.systemStatusBar()
            self._status_item = self._status_bar.statusItemWithLength_(
                NSVariableStatusItemLength
            )
            self._status_item.setTitle_("T: idle")
            self._status_item.setHighlightMode_(True)

            # Build menu
            menu = NSMenu.alloc().init()

            stop_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Stop Automation", "stopFromMenu:", ""
            )
            stop_item.setTarget_(self._menu_delegate)
            menu.addItem_(stop_item)

            show_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Show Window", "showFromMenu:", ""
            )
            show_item.setTarget_(self._menu_delegate)
            menu.addItem_(show_item)

            separator = NSMenuItem.separatorItem()
            menu.addItem_(separator)

            quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit Taey-Ed", "quitFromMenu:", "q"
            )
            quit_item.setTarget_(self._menu_delegate)
            menu.addItem_(quit_item)

            self._status_item.setMenu_(menu)
            self.logger.info("Menu bar status icon created")

        except Exception as e:
            self.logger.warning(f"Could not create menu bar icon: {e}")
            self._status_item = None

    def _update_menu_bar_title(self, title: str):
        """Thread-safe menu bar title update."""
        try:
            if hasattr(self, '_status_item') and self._status_item:
                # NSStatusItem.setTitle_ must be called from main thread
                self.root.after(0, lambda: self._status_item.setTitle_(title))
        except Exception:
            pass  # Non-fatal

    def _bring_to_front(self):
        """Bring the main window to front."""
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", False))

    # =========================================================================
    # Main Loop
    # =========================================================================

    def run(self):
        """Start the application main loop."""
        self.root.mainloop()

    def destroy(self):
        """Cleanup on exit."""
        # Remove global hotkey monitor
        if hasattr(self, '_hotkey_monitor') and self._hotkey_monitor:
            try:
                from AppKit import NSEvent
                NSEvent.removeMonitor_(self._hotkey_monitor)
            except Exception:
                pass
        # Remove status bar item
        if hasattr(self, '_status_item') and self._status_item:
            try:
                from AppKit import NSStatusBar
                NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            except Exception:
                pass

    def _on_close(self):
        """Graceful close: stop pipeline, wait for abandon-consultation, exit.

        Wired to WM_DELETE_WINDOW. Without this hook, clicking red X just
        tore down the window while the pipeline kept polling Spark, leaving
        any active consultation pending forever.
        """
        import time
        self.logger.info("Window close: shutting down")
        # Tell pipeline to stop. It will send /api/v1/abandon_consultation/{id}
        # in its exit path if there's an active consultation.
        self.stop_event.set()
        # Unblock pipeline if it was waiting on user input
        if not self._chat_event.is_set():
            self._chat_event.set()
        # Give the pipeline thread up to 5s to exit cleanly so the abandon
        # POST has time to land. Daemon threads die with the process — no
        # graceful abandon if we exit too fast.
        if self._is_running:
            self.logger.info("Waiting up to 5s for pipeline to abandon active consultation...")
            for _ in range(50):
                if not self._is_running:
                    break
                time.sleep(0.1)
            if self._is_running:
                self.logger.warning("Pipeline thread did not exit in 5s; abandon may not have landed")
        # Tk + AppKit teardown
        self.destroy()
        try:
            self.root.destroy()
        except Exception:
            pass
        # Force-exit the process. Without this, AppKit/NSApplication can
        # keep the process alive (the hotkey monitor / status bar item).
        import sys
        sys.exit(0)


if __name__ == "__main__":
    app = TaeyEdWindow()
    app.run()
