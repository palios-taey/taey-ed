# Taey-Ed — System Flow & Scenarios
**Updated**: February 26, 2026
**Purpose**: Complete flow documentation for review before architectural changes

---

## How the System Works (Overview)

The Mac app captures what's on screen (the accessibility tree — a structured description of every UI element) and sends it to Spark. Spark figures out what kind of screen it is, decides what to do, and sends back instructions (a behavior tree). The Mac executes those instructions. This loop repeats every few seconds.

```
Mac captures screen
       │
       ▼
  Send to Spark ──────► "What kind of screen is this?"
                              │
                              ├─ Known deterministic screen (VIDEO, ARTICLE)
                              │    └─ Return stored instructions → Mac executes
                              │
                              ├─ Known dynamic screen (EXERCISE, NAVIGATION, etc.)
                              │    └─ Send to Gemini → build fresh instructions → Mac executes
                              │
                              └─ Unknown screen
                                   └─ Ask Gemini to classify → then handle as above
                              │
  Mac executes ◄────── Instructions returned
       │
       ▼
  Did it work?
       │
       ├─ YES → capture next screen → repeat
       └─ NO  → report failure with context → Spark/Gemini tries different approach
```

---

## Screen Types — Master Categories and Variants

There are 6 universal master categories. Each platform will have its own specific variants (sub-states) under these categories. The master category determines handling strategy (deterministic vs dynamic). Variants are platform-specific and discovered/labeled by Gemini as new screens are encountered.

| Master Category | Handling | Example Variants (differ by platform) |
|----------------|----------|---------------------------------------|
| **VIDEO** | Deterministic — stored BTs | Unstarted, playing, complete, etc. |
| **ARTICLE** | Deterministic — stored BTs | Content visible, fully read, etc. |
| **EXERCISE** | Dynamic — Gemini every time | Radio, checkbox, text input, matching, assessment, etc. |
| **NAVIGATION** | Dynamic — Gemini every time | Course list, module list, unit page, etc. |
| **TRANSITION** | Dynamic — Gemini every time | Completion screen, interstitial, modal, etc. |
| **UNKNOWN** | Escalate | Anything unrecognized |

**How variants work:**
- Variants follow the pattern `{CATEGORY}_{DESCRIPTOR}` (e.g., VIDEO_PLAYING, EXERCISE_RADIO)
- Gemini assigns the variant name when it classifies or builds a BT
- The system stores recognition at the variant level (so each sub-state has its own fingerprint)
- New variants are created naturally as the system encounters new screen layouts
- The architecture supports any number of variants under each master category — they're not predefined

**Key distinction:**
- VIDEO and ARTICLE variants have the same layout and behavior every time → stored BTs work
- EXERCISE, NAVIGATION, TRANSITION variants are recognized (so Gemini gets type context) but need fresh Gemini instructions every time because the content changes

---

## Screen Recognition

When a screen arrives, Spark needs to know what type it is. Recognition uses a fingerprinting system:

1. **Extract fingerprint**: Pull out the stable UI elements (button labels, menu items, form fields). Ignore variable content (paragraph text, images, headings).

2. **Filter out noise**: Elements that appear on EVERY screen (browser buttons, platform nav bar) are subtracted. What's left are the elements that make THIS screen unique.

3. **Compare**: Check the unique elements against all previously seen screens. If there's a 70%+ match, it's the same type.

4. **Learning**: Every new screen type gets stored. The more screens are seen, the better the noise filter gets (the "common elements" set becomes more accurate).

**Recognition is stored centrally on Spark** because platform layouts are the same for all users. A Coursera exercise screen looks the same whether Jesse or someone else is taking the course.

---

## Detailed Flow — All Scenarios

### Scenario 1: Known Deterministic Screen (VIDEO, ARTICLE)

```
Mac sends screen
    │
    ▼
Spark recognizes it (fingerprint match)
    │
    ▼
Screen type has stored behavior tree?
    │
    ├─ YES (VIDEO or ARTICLE) ─────► Return stored BT
    │                                      │
    │                                      ▼
    │                                Mac executes BT
    │                                      │
    │                                      ▼
    │                                Screen changes?
    │                                      │
    │                                ├─ YES → advance to next screen
    │                                └─ NO  → [See Scenario 6: Stuck]
    │
    └─ NO (needs Gemini) ──────────► [See Scenario 2]
```

**VIDEO sub-states:**

```
VIDEO_UNSTARTED
    │ Click Play
    ▼
VIDEO_PLAYING
    │ Poll every 30 seconds (check if video player still visible)
    │
    ├─ Video still playing → keep polling
    │
    └─ Video ended (player gone)
         │
         ▼
    VIDEO_COMPLETE
         │ Mark as completed → advance to next item
         ▼
    Next screen
```

**ARTICLE flow:**

```
ARTICLE detected
    │
    ├─ Scroll through content
    ├─ Extract text (for knowledge base)
    ├─ Mark as completed
    └─ Advance to next item
```

### Scenario 2: Known Dynamic Screen (EXERCISE, NAVIGATION, TRANSITION)

```
Mac sends screen + screenshot
    │
    ▼
Spark recognizes screen TYPE (e.g., "EXERCISE")
    │
    ▼
Send tree + screenshot + screen type to Gemini 2.5 Pro
    │
    ▼
Gemini analyzes the specific content and builds instructions:
  - For EXERCISE: "This is a multiple choice question about X.
    Read the question, determine the answer, click option B, click Submit"
  - For NAVIGATION: "This is a course page. Click the first incomplete item: 'Module 3'"
  - For TRANSITION: "This is a completion screen. Click 'Continue to next lesson'"
    │
    ▼
Mac executes the instructions
    │
    ▼
Did the screen change as expected?
    │
    ├─ YES → mark these instructions as validated → next screen
    └─ NO  → [See Scenario 5: Failed Instructions]
```

### Scenario 3: First-Time Screen (Never Seen Before)

```
Mac sends screen + screenshot
    │
    ▼
Spark doesn't recognize the fingerprint (no match)
    │
    ▼
Send to Gemini for classification:
  "What type of screen is this? VIDEO, ARTICLE, EXERCISE, NAVIGATION, TRANSITION, or UNKNOWN?"
    │
    ▼
Gemini returns screen type
    │
    ▼
Store the fingerprint with this type (so it's recognized next time)
    │
    ├─ VIDEO or ARTICLE → store deterministic BT → execute
    │
    ├─ EXERCISE/NAVIGATION/TRANSITION → send to Gemini for instructions → execute
    │
    └─ UNKNOWN → [See Scenario 4: Unknown Screen]
```

### Scenario 4: Unknown Screen (Gemini Can't Classify)

```
Gemini says "UNKNOWN"
    │
    ▼
Try Gemini BT builder anyway:
  "I don't know what type this is, but here's the full tree and screenshot.
   What should the user do?"
    │
    ├─ Gemini figures it out → returns instructions → execute
    │
    └─ Gemini can't figure it out
         │
         ▼
    ASK THE USER via chat:
      System: "I don't recognize this screen. What should I do here?"
      User types response
         │
         ▼
    Send user's guidance + tree + screenshot to Gemini:
      "The user says to click 'Continue'. Build instructions for that."
         │
         ▼
    Gemini builds instructions incorporating user guidance → execute
```

### Scenario 5: Failed Instructions (BT Didn't Work)

This is the critical failure/retry path. Currently there's a gap here that we need to fix.

**Current behavior (broken):**
```
Mac executes instructions → they fail
    │
    ▼
Mac reports: "It failed" + debug log snippet
    │
    ▼
Spark does NOT know what instructions were tried
    │
    ▼
If user provides guidance → Gemini builds new instructions
    (but Gemini doesn't know what was already tried → may repeat same mistake)
    │
If no guidance → STOP, ask user
```

**Desired behavior (the fix):**
```
Mac executes instructions → they fail
    │
    ▼
Mac reports:
  - Current tree + screenshot (what the screen looks like NOW)
  - The instructions that were tried (the failed BT)
  - What went wrong (debug log)
    │
    ▼
Spark sends ALL of this to Gemini:
  "You previously told the user to do this: [failed BT].
   It didn't work. The screen still looks like this: [tree + screenshot].
   Here's what happened: [debug log].
   Try a DIFFERENT approach."
    │
    ▼
Gemini builds genuinely different instructions (knows what NOT to repeat)
    │
    ▼
Mac executes new instructions
    │
    ├─ SUCCESS → store corrected approach → continue
    │
    └─ FAILED AGAIN → [See Scenario 8: Escalation]
```

### Scenario 6: Screen Stuck (Instructions Ran But Nothing Changed)

```
Mac executes instructions
    │
    ▼
Screen hash is IDENTICAL to before (nothing happened)
    │
    ▼
The instructions had no effect — ONE TRY ONLY, don't retry same thing
    │
    ▼
Delete the stored instructions for this screen (they're proven broken)
    │
    ▼
ASK THE USER via chat:
  System: "I tried to [action] on [screen type] but nothing happened.
           The screen didn't change. What should I do here?"
    │
    ▼
[See Scenario 9: User Chat Resolution]
```

### Scenario 7: Wrong Answer Detected

```
Mac executes quiz instructions (selects answer, clicks Submit)
    │
    ▼
Screen changes... but it's THE SAME quiz screen again
(Same structure, same hash — the answer was wrong)
    │
    ▼
WRONG ANSWER — ONE TRY ONLY, don't guess again
    │
    ▼
Delete the stored instructions for this screen
    │
    ▼
ASK THE USER via chat:
  System: "I answered [screen type] incorrectly.
           Tell me what the correct action is for this screen."
    │
    ▼
[See Scenario 9: User Chat Resolution]
```

**Note:** If the quiz advances to a DIFFERENT question (different hash), that's progress, not a wrong answer. The system correctly distinguishes between "same question re-presented" (wrong answer) and "next question" (progress).

### Scenario 8: Escalation Chain

When automated resolution fails repeatedly:

```
Attempt 1: Gemini builds instructions
    │
    └─ FAILED
         │
         ▼
Attempt 2: Gemini builds DIFFERENT instructions (with failure context)
    │
    └─ FAILED
         │
         ▼
Attempt 3: Escalate to research mode
    Spark Claude uses Perplexity Deep Research to study the platform's
    behavior for this screen type, then builds instructions informed
    by the research
    │
    └─ FAILED
         │
         ▼
Attempt 4+: Escalate to user
    System: "I've tried 3 different approaches and none worked.
             I need your help. Here's what I see and what I've tried..."
    │
    ▼
[See Scenario 9: User Chat Resolution]
```

### Scenario 9: User Chat Resolution

The chat window is the user's interface for helping the system when it gets stuck.

**System asks for help:**
```
System: "I can't find the Submit button on this exercise.
         What's it labeled on this platform?"
    │
    ▼
User: "It's called 'Check' on Khan Academy"
    │
    ▼
System sends user guidance + current tree + screenshot to Gemini:
  "The user says the submit button is labeled 'Check'.
   Build instructions that use 'Check' instead of 'Submit'."
    │
    ▼
Gemini builds corrected instructions → Mac executes
    │
    ├─ SUCCESS
    │    │
    │    ▼
    │  System: "Fixed! That worked."
    │  (Store corrected approach for this screen type)
    │
    └─ STILL FAILED
         │
         ▼
    System: "That still didn't work. Can you describe what you see
             and what should happen?"
         │
         ▼
    [Multi-turn conversation until resolved or user gives up]
```

**User reports a problem proactively:**
```
User types: "It skipped that video"
    │
    ▼
Message queued and included in next cycle
    │
    ▼
System acknowledges and adjusts:
  System: "Got it — I'll go back and watch that video."
```

**User provides correction:**
```
User types: "The answer was B, not C"
    │
    ▼
System uses correction to improve future behavior
```

### Scenario 10: Content Completion Flow

When a piece of content (video, article) finishes:

```
Content completed (video ended or article scrolled through)
    │
    ▼
Try to mark as completed (click platform's "done" button)
    │
    ▼
Advance to next item (click platform's "next" button)
    │
    ▼
New screen loads → recognize and handle
```

**Platform differences:**
- Coursera: "Mark as completed" → "Go to next item"
- Khan Academy: Auto-completes on view, close modal with Escape
- edX: "Mark as complete" → "Next"
- Each platform has different button labels for the same action

### Scenario 11: Screenshot Request

Some decisions need visual context that the tree alone can't provide:

```
Spark needs to classify an unknown screen
    OR
Spark needs Gemini to determine what to click on a navigation screen
    │
    ▼
Spark: "I need a screenshot to make this decision"
    │
    ▼
Mac captures screenshot → resends request with screenshot attached
    │
    ▼
Spark proceeds with classification or BT building
```

### Scenario 12: Active Consultation (Async Resolution)

For complex problems that take time to resolve:

```
Screen is too complex for immediate resolution
    │
    ▼
Spark creates a consultation request
  (Stores tree, screenshot, context for review)
    │
    ▼
Mac receives "consulting" response → polls every 5 seconds
    │
    ▼
Spark Claude (or Perplexity, or user) resolves the consultation
  → writes response with new instructions
    │
    ▼
Mac's next poll picks up the resolution → executes new instructions
```

---

## What Gets Stored Where

### On Spark (Central, Persistent)
- **Screen fingerprints** (signatures): Layout recognition data. Same across all users because platform layouts are universal. Stored in persistent directory.
- **Behavior trees for VIDEO and ARTICLE**: Deterministic instructions that work the same every time. No reason to rebuild these.
- **Platform knowledge**: RESEARCH.md files documenting each platform's quirks, navigation patterns, button labels.
- **Chat history**: Redis-backed message history per platform.

### On Mac (Per User, Local)
- **Recent interaction buffer**: Last few screens, instructions received, and results. This is what gets sent back to Spark when something fails so Gemini can see what was already tried.
- **Screenshots and trees**: Captured locally, sent to Spark as needed, not stored permanently on either side.

### Temporary on Spark (During Processing)
- **Consultation files**: Screenshots, trees, and metadata while a consultation is in progress. Rolling cleanup keeps only the 2 most recent completed consultations.
- **Review files**: Similar rolling cleanup for action reviews.

---

## Models Used

| Model | Purpose | When Called |
|-------|---------|------------|
| **Gemini 2.5 Pro** (default) | Screen classification, BT building, complex reasoning | Every non-deterministic screen, every failure retry |
| **Gemini 2.5 Flash** | Answer generation for exercises (Q&A) | Every exercise screen (answer the question) |
| **Gemini 2.5 Flash-Lite** | Fallback for Flash on rate limits | Only when Flash returns 429 |
| **Claude CLI (sonnet)** | Final fallback for Q&A | Only when all Gemini models fail |

---

## Chat Message Types

The chat panel shows these kinds of messages:

| Type | Direction | Example |
|------|-----------|---------|
| **Status** | System → User | "Executing VIDEO automation" |
| **Status** | System → User | "Content completed — advancing to next item" |
| **Question** | System → User | "Screen unchanged after action. What should I do here?" |
| **Question** | System → User | "Wrong answer detected. Tell me the correct action." |
| **Answer** | User → System | "The submit button is called Check" |
| **Answer** | User → System | "It skipped that video" |
| **Status** | System → User | "Fixed! That worked." |

---

## New Platform Onboarding (Perplexity Deep Research)

When a platform is used for the first time (no RESEARCH.md exists), the system cannot build reliable instructions because it doesn't know the platform's quirks — button labels, navigation patterns, modal behaviors, edge cases.

```
First request for a new platform (e.g., edX)
    │
    ▼
Spark checks: does RESEARCH.md exist for this platform?
    │
    ├─ YES → proceed normally (platform knowledge available)
    │
    └─ NO → RESEARCH REQUIRED FIRST
              │
              ▼
         Spark Claude (the taey-ed Claude session) is notified:
           "No research exists for edX. You must do Perplexity
            Deep Research BEFORE mapping any screens."
              │
              ▼
         Spark Claude uses taey's hands MCP tools:
           1. Open Perplexity (taey_inspect → taey_set_map)
           2. Enable Deep Research mode
           3. Send research query about the platform:
              "How does edX course navigation work?
               What are the screen types, button labels,
               quiz formats, video player controls?"
           4. Wait for Deep Research response (2-5 minutes)
           5. Extract the full response
           6. Save to spark/platforms/edx/RESEARCH.md
              │
              ▼
         RESEARCH.md now exists with:
           - Platform navigation patterns
           - Button labels and their functions
           - Quiz/exercise formats
           - Video player behavior
           - Known edge cases and gotchas
              │
              ▼
         NOW proceed with screen mapping
         (Gemini uses RESEARCH.md as context for better BTs)
```

**Escalation also uses Perplexity:**
When the normal escalation chain reaches tier 2 (2+ failed attempts by Spark Claude), the system escalates to Perplexity Deep Research to study the specific screen type that's causing problems. This provides Gemini with richer platform-specific context for building a different approach.

```
Tier 1 (attempts 0-1): Spark Claude + Gemini (normal)
Tier 2 (attempts 2+):  Perplexity Deep Research → then Gemini with research context
Tier 3 (attempts 3+):  User escalation via chat
```

---

## Content Extraction and Storage

Content extraction is already built (current and previous versions). As the system navigates through course content, it extracts and stores material for two purposes: (1) building a knowledge base for answering questions, and (2) preserving the learning content. The key is ensuring the BTs produced by Gemini include the appropriate extraction steps.

### What Gets Extracted

| Content Type | How It's Extracted | When |
|-------------|-------------------|------|
| **Article/lesson text** | Walk the accessibility tree, collect all AXStaticText elements within the web content area (excluding browser chrome) | During ARTICLE screen handling |
| **Questions** | Parse tree for question markers (text with "?" or "___"), collect surrounding context | During EXERCISE screen handling |
| **Answer options** | Collect button text (AXButton), radio/checkbox labels, dropdown items | During EXERCISE screen handling |
| **Generated answers** | Gemini/Claude generates answer based on question + options + knowledge base context | After question extraction, before clicking |
| **Image descriptions** | Screenshot regions sent to Gemini Vision for OCR, diagram description, equation extraction (LaTeX) | When images detected in content area |
| **Dropdown menu items** | Walk AXMenuItem elements when dropdown is open | During `discover_menu` BT action |

### What's NOT Currently Extracted (Gaps)

| Missing Content | Impact | Notes |
|----------------|--------|-------|
| **Video transcripts** | No text record of lecture content | Transcripts are often available in the player UI (Khan Academy shows them, Coursera has a Transcript tab). Not currently extracted. |
| **Dropdown options after use** | Options discovered but discarded after clicking | `discover_menu` finds them for immediate use but doesn't persist them for future reference |
| **Assessment results/scores** | No tracking of which answers were correct | Q&A pairs are stored but correctness is not verified after submission |
| **Course structure/syllabus** | No map of what's in the course | Navigation screens show the structure but it's not captured as metadata |

### Where Content Is Stored

```
Mac (Local per course):
  ~/deeptutor_data/{platform}_{course_id}.db (SQLite)
    │
    ├─ content table
    │    ├─ Extracted text (JSON array of strings)
    │    ├─ Image descriptions (JSON array of {description, purpose})
    │    ├─ Embedding vectors (for future semantic search)
    │    ├─ Screen type, lesson name
    │    └─ Timestamp
    │
    ├─ qa_pairs table
    │    ├─ Question text
    │    ├─ Generated answer
    │    ├─ Question type (solve_choice, solve_checkbox, etc.)
    │    ├─ Correct flag (optional, not reliably set)
    │    └─ Timestamp
    │
    ├─ courses table
    │    ├─ Platform, course name, subject
    │    └─ Timestamp
    │
    └─ checkpoints table
         ├─ Platform, course_id, screens completed
         ├─ Last screen type, last action
         └─ Updated timestamp (for crash recovery)
```

### Knowledge Base Context (Q&A Support)

When the system encounters a question, it builds context from previously extracted content:

```
Exercise screen detected
    │
    ▼
Extract question text + answer options from tree
    │
    ▼
Search knowledge base:
  1. Keyword search on extracted content (SQLite LIKE query)
  2. Fall back to most recent 5 content extracts
    │
    ▼
Build prompt for Gemini:
  "Here's the question: [question]
   Here are the options: [A, B, C, D]
   Here's relevant context from the course: [KB results]
   What's the correct answer?"
    │
    ▼
Gemini returns answer → Mac clicks the option → submits
    │
    ▼
Store Q&A pair in SQLite for future reference
```

### Extraction in the BT Flow

Content extraction is woven into behavior tree execution:

```
ARTICLE BT:
  1. scroll (expose content)
  2. extract_text (walk tree → collect text → store in SQLite)
  3. extract_images (screenshot regions → Gemini Vision → store descriptions)
  4. mark_complete → advance

EXERCISE BT:
  1. extract_question (find question + options + reference text)
  2. send_to_llm (build KB context + send to Gemini → get answer)
  3. find_and_click (click the answer option)
  4. find_and_click (click Submit/Check)
  5. store_qa (persist question + answer to SQLite)

VIDEO BT:
  1. click Play
  2. video_poll (wait for completion)
  3. [MISSING: extract transcript]
  4. mark_complete → advance
```

---

## Current Issues That Need Fixing

### 1. Failed BT not sent back to Gemini
When instructions fail, Mac reports the failure but doesn't include the actual instructions that were tried. Gemini has no way to know what was already attempted and may suggest the same broken approach again.

**Fix needed:** Mac includes the failed BT in its failure report. Spark passes it to Gemini with "don't do this again."

### 2. Stored BTs being reused for dynamic screens
Currently, when Gemini builds a BT for an exercise screen, it gets stored with the fingerprint and reused next time the same layout appears. But the content is different (different question, different options), so the stored BT has wrong answers baked in.

**Fix needed:** Only store reusable BTs for VIDEO and ARTICLE. For everything else, store the recognition (so we know the screen TYPE) but always ask Gemini for fresh instructions.

### 3. Hardcoded platform button names
"Mark as completed", "Go to next item", "Submit", "Check" are hardcoded in templates and prompts. These are Coursera/Khan Academy specific and won't work on other platforms.

**Fix needed:** Button names should come from platform configuration or be determined by Gemini from the actual screen content.

### 4. Gemini model upgrade
Currently using Gemini 2.5 Flash for classification and Gemini 3 Pro Preview for BT building. Flash isn't smart enough for reliable BT construction.

**Fix needed:** Default to Gemini 2.5 Pro for classification and BT building.

### 5. Screenshots and trees accumulating on Spark
Consultation and review files stored in /tmp/ on Spark. Should be minimal and rolling.

**Status:** Rolling cleanup added (keeps last 2 completed). Mac should maintain its own recent history and send as needed.

### 6. Extraction handlers exist but must be wired into BTs
All extraction handlers are already built (extract_text, extract_question, send_to_llm, store_qa, extract_images, discover_menu, video_poll). The Gemini BT builder prompt needs to know about these handlers so it includes extraction steps in the BTs it generates — e.g., extracting transcripts from video screens, persisting dropdown options, capturing course structure from navigation screens.

---

## Desired End State

1. Mac captures screen → sends tree (+ screenshot if needed) to Spark
2. Spark recognizes screen type via fingerprint matching
3. If VIDEO or ARTICLE → return stored BT (instant, no API call)
4. If anything else → send to Gemini 2.5 Pro with screen type context → return fresh BT
5. If failed → Mac sends failed BT + context → Gemini sees what didn't work → builds different approach
6. If still failing → escalate through chat → user helps → resolved
7. Learning: recognition improves with every new screen. Deterministic BTs persist. Dynamic screens always get fresh Gemini analysis.
