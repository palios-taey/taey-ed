# Khan Academy -- Platform Navigation Research

**Source**: Perplexity Deep Research (February 11, 2026)
**Purpose**: Navigation rules for Taey-Ed V7 YAML screen mapping

---

## 1. Platform Overview

Khan Academy is a free, mastery-based learning platform. Content is structured hierarchically:

**Course > Unit > Lesson > Content Items**

### Content Types

| Type | Description | Completion |
|------|-------------|------------|
| **Video** | Educational videos (1-15 min) | Must watch to absolute end, no skipping |
| **Article** | Text-based reading content | Open/access = complete |
| **Exercise** | Practice problems (4-7 questions per skill) | Answer all questions using Check button |
| **Quiz** | Unit-level assessment (min 5 questions) | Complete all questions |
| **Unit Test** | Comprehensive unit assessment (min 9 questions) | Complete all questions |
| **Course Challenge** | Course-wide assessment (30 questions) | Complete all questions |
| **Mastery Challenge** | Adaptive review (time-gated, 12h cooldown) | Complete all questions |

All core content is **free**. Only Khanmigo (AI tutor) and Districts features are paid.

---

## 2. Navigation Structure

### 2.1 Course Homepage

The course homepage shows all units in a grid. Each unit displays:
- Unit title and description
- Unit mastery percentage (e.g., "Unit mastery: 0%")
- A mastery progress visualization: a grid of small colored squares (one per skill), plus lightning bolt icons (quizzes) and star icons (unit tests)
- Lesson titles with content items beneath them
- "Get started" link on units not yet begun

### 2.2 Unit Page

Clicking a unit opens the unit page showing all lessons and content items in curriculum order. Items are organized by lesson with completion indicators.

### 2.3 Left Sidebar (Content Navigation Panel)

- Opens by default, can be hidden by clicking a left-pointing arrow
- Top section: Assignments (if any), sorted by due date
- Below assignments: All content items for the current unit/lesson, listed in chronological/curriculum order
- Each item shows its type icon and completion status
- Left/right arrow buttons at lesson boundaries allow navigating to previous/next lessons within the unit

### 2.4 Navigation Buttons and Controls

| Button | Location | Behavior |
|--------|----------|----------|
| "Up next" | Course/unit page | **MASTERY-ADAPTIVE** -- may skip content! |
| "Get started" | Unit on course page | Opens first item in mastery order (not necessarily first in curriculum) |
| "Next assignment" | Sidebar top | Navigates to assignment content (can be from ANY unit) |
| Left/Right arrows | Sidebar lesson boundaries | Navigate previous/next lesson within unit |
| Close (X) | Modal top-right | Dismiss video/article modal, return to unit page |
| "Check" | Exercise page bottom-right | Submit answer for current question |
| "Skip" | Exercise page | Skip current question (counts as incorrect) |

---

## 3. Completion / Progress Model

### 3.1 Mastery Levels (Per Skill)

| Level | Color | Description |
|-------|-------|-------------|
| Not Started | Gray/empty | No attempts |
| Attempted | Orange/red | Tried but not yet familiar |
| Familiar | Teal/blue | Some understanding demonstrated |
| Proficient | Green | Good understanding |
| Mastered | Blue/dark | Full mastery demonstrated |

### 3.2 Video Completion Indicators

- **Not viewed**: No colored bar beneath the video icon in sidebar/listings
- **Incomplete / In progress**: Half-colored bar at the bottom of the video icon
- **Completed**: Full colored bar at the bottom + checkmark above the icon
- Must watch every second -- skipping any section marks it incomplete
- Playback speed up to 2x is fine (does not affect completion)
- Must be logged in for completion to register
- Must let the video play to the absolute end (closing early = incomplete)
- Watching at 90%+ of the video earns completion for assignment purposes

### 3.3 Article Completion

- Checkmark appears when the article has been accessed (opened/viewed)
- No partial completion state -- it's either accessed or not
- For assignment purposes, simply opening the article counts as completion

### 3.4 Exercise / Quiz / Test Completion

- Score is calculated after answering all questions
- After completion: a score card appears showing skill level changes (leveled up, leveled down, or stayed same for each skill)
- The score card may recommend lessons based on performance

### 3.5 Unit and Course Mastery

- Unit mastery %: shown on course page and unit page header (e.g., "Unit mastery: 0%")
- Course mastery %: shown on course page header
- Progress visualization: grid of colored squares (1 per skill), each colored by mastery level

### 3.6 Expected Accessibility Tree Representations

| Element | Expected AX Role | Text Content |
|---------|-----------------|-------------|
| Skill mastery square | AXLink | "Understand: [topic]: unfamiliar" or "Apply: [topic]: proficient" |
| Quiz icon | AXLink | "[Unit name]: Quiz N" |
| Unit test icon | AXLink | "[Unit name]: Unit test" |
| Video checkmark | AXImage | Completion indicator image |
| Mastery percentage | AXStaticText | "Unit mastery: N%" |
| "Up next for you!" | AXStaticText | "Up next for you!" |
| Unit link | AXLink | "Unit N [title]" or "UNIT N [title]" |

---

## 4. Content Ordering Rules

### 4.1 Within a Lesson

Content items appear in this order:
1. Videos (Learn)
2. Articles (Learn)
3. Exercises (Practice)

Complete all Learn items before Practice items.

### 4.2 Within a Unit

Lessons are presented sequentially:
1. Lesson 1: Videos > Articles > Exercises
2. Lesson 2: Videos > Articles > Exercises
3. ...
4. Quiz 1 (after a group of lessons)
5. More lessons...
6. Quiz 2
7. Unit Test (at the end)

### 4.3 Within a Course

Units are numbered sequentially (Unit 1, Unit 2, ...). Complete units in order.
Course Challenge available after sufficient progress across all units.

### 4.4 How "Up Next" / "Start" / "Resume" Behave

**CRITICAL**: "Up next" follows the **mastery-adaptive algorithm**, NOT curriculum order. It may:
- Skip videos/articles if it determines the student should focus on exercises
- Jump to an exercise in a different lesson within the unit
- Suggest a quiz or unit test if enough skills show progress
- Recommend a Mastery Challenge if unlock conditions are met

### 4.5 Rule for Taey-Ed

**DO NOT rely on "Up next" for sequential completion.** It may skip Learn content (videos/articles) that the student hasn't viewed yet. Instead, scan the sidebar or unit page for the first incomplete item in curriculum order and click that directly.

---

## 5. Buttons and CTAs

### 5.1 Exercise Page Buttons

| Button | Safe to Click? | What It Does |
|--------|---------------|-------------|
| "Check" | YES | Submits current answer, shows correct/incorrect feedback |
| "Next" | YES (after Check) | Advances to next question |
| "Skip" | AVOID | Skips question, counts as incorrect |
| "Start over" | AVOID | Only appears after 2 wrong. Resets exercise, no points awarded |
| "Show hints" | OK | Reveals progressive hints (doesn't affect mastery) |

### 5.2 Video Page Buttons

| Button | Safe to Click? | What It Does |
|--------|---------------|-------------|
| Play/Pause | YES | Controls video playback |
| Speed (1x, 1.5x, 2x) | YES | Changes speed, completion still counts at 2x |
| Transcript toggle | YES | Shows/hides transcript text |
| "Skip to content" | AVOID | May skip video completion |
| Close (X) | ONLY after completion | Dismisses video modal |

### 5.3 Unit/Course Page Buttons

| Button | Safe to Click? | What It Does |
|--------|---------------|-------------|
| "Get started" | CAUTION | Opens mastery-recommended item, not necessarily first |
| "Up next" / "Up next for you!" | CAUTION | Mastery-adaptive, may skip content |
| Unit link (e.g., "Unit 1") | YES | Opens unit page with all content listed |
| Skill link (e.g., "Understand: atomic structure") | YES | Opens specific content item |
| Quiz/Test link | YES but check prereqs | Opens assessment |
| "Start Course challenge" | YES but check prereqs | Opens 30-question course assessment |

---

## 6. Progress-Aware Navigation Guidance

### 6.1 Core Rules for Taey-Ed Agents

1. **NEVER click "Up next" blindly.** The mastery system may skip videos and articles. Always check the sidebar or unit listing for incomplete Learn items first.
2. **Scan the sidebar from top to bottom.** The first item without a completion indicator (checkmark, colored bar) is the item Taey-Ed should navigate to.
3. **Complete Learn items BEFORE Practice items.** Within each lesson, all videos and articles should show completion indicators before starting exercises. This matches the platform's intended pedagogical order.
4. **Videos must be watched to completion.** Use video_poll -- never click "Skip" or navigate away. Wait for the video to finish naturally (full colored bar + checkmark appears).
5. **Articles just need to be opened.** Opening/accessing an article is sufficient for completion. Capture content via extraction, then look for the checkmark.
6. **Exercises require answering ALL questions.** Don't click "Start over" (only appears after 2 wrong). Don't click "Skip" unless stuck (counts as incorrect). Use "Check" for each question.
7. **Quizzes and Unit Tests should only be started after all lesson exercises in the unit are complete.** Check that practice exercises show Familiar or higher mastery levels before beginning assessments.
8. **Course Challenge should only be started after all units show meaningful progress.** It samples skills from the entire course.

### 6.2 How to Determine First Incomplete Item

From the course overview page or unit page:
1. Look at skills list (AXLink elements with text like "Understand: [topic]: unfamiliar")
2. The mastery level suffix tells you status: "unfamiliar" = not started, "familiar" = in progress, "proficient"/"mastered" = done
3. Find the first "unfamiliar" skill in curriculum order
4. Click that skill link to open the content

From the sidebar (when inside a lesson):
1. Scan list items from top to bottom
2. Look for items without checkmarks or colored bars
3. Click the first incomplete item

### 6.3 Interpreting Sidebar Status

| Indicator | Meaning | Action |
|-----------|---------|--------|
| No icon/bar | Not started | Click to start |
| Half bar | In progress (video) | Resume watching |
| Full bar + checkmark | Complete | Skip, move to next |
| Colored mastery text | Exercise with mastery level | Check level before deciding |

### 6.4 Decision Tree for Navigation

```
On Course Page:
  1. Click first unit with incomplete skills (UNIT N link)
  2. On unit page, find first incomplete lesson content
  3. Click specific content item (NOT "Up next")

On Unit/Lesson Page:
  1. Check sidebar for first incomplete item
  2. Is it a video? -> Click to open, use video_poll
  3. Is it an article? -> Click to open, extract content
  4. Is it an exercise? -> Click to open, answer all questions
  5. All items complete? -> Check for quiz/test, then next lesson
```

---

## 7. Accessibility / Tree Expectations

### 7.1 Khan Academy's Accessibility Posture

- Khan Academy has adopted WCAG 2.1 Level AA as their baseline
- They partner with external accessibility experts for continuous improvement
- Contact: accessibility@khanacademy.org

### 7.2 Screen Reader Support

Tested with:
- JAWS (Windows) + Firefox
- NVDA (Windows) + Firefox
- VoiceOver (Mac) + Safari

### 7.3 Known Accessibility Features

- Settings > Accessibility: Options to hide visually-dependent content and remove color from videos
- Videos muted by default for new users (screen reader friendly -- prevents audio overlap with captions)
- Color contrast improvements meeting WCAG standards
- Keyboard navigation for modals
- Rebuilt graph exercises for full keyboard + screen reader support (using SVG with ARIA)
- Dynamic screen reader notifications for graph state changes

### 7.4 Expected AX Tree Structure (Chrome on macOS)

| Page Element | AX Role | Identifying Text |
|-------------|---------|-----------------|
| Course title | AXHeading | "High school chemistry" |
| Unit link in sidebar | AXLink | "UNIT N [title]" or "Unit N" |
| Skill link | AXLink | "Understand: [topic]: [level]" or "Apply: [topic]: [level]" |
| Quiz link | AXLink | "[Unit]: Quiz N" |
| Unit test link | AXLink | "[Unit]: Unit test" |
| "Up next for you!" | AXStaticText | "Up next for you!" |
| Mastery points | AXStaticText | "N,NNN possible mastery points" |
| Course mastery grid | AXList | "units" |
| Skills grid | AXList | "Skills" |
| Breadcrumbs | AXGroup | "Breadcrumbs" |
| Search | AXButton | "Search for courses, skills, and videos" |
| User menu | AXGroup | "user menu" |

### 7.5 Content Type Identification in Tree

Skills text in the tree encodes both topic and mastery level:
- "Understand: atomic structure: unfamiliar" = Not started concept understanding
- "Apply: isotopes: familiar" = In-progress application skill
- "Atoms, isotopes, and ions: Quiz 1" = Quiz assessment
- "Atoms, isotopes, and ions: Unit test" = Unit test

### 7.6 React SPA Implications for Automation

- **No full page reloads**: Navigation between content items happens via client-side routing. The accessibility tree will update in-place rather than creating a completely new tree.
- **Dynamic content loading**: After clicking a link, there may be a brief loading state before content appears. Use wait_for_element to detect when the new content has rendered.
- **Modal behavior**: Learn items (videos/articles) from the unit page open as modals. The modal will overlay the unit page content. Look for modal-specific elements (close button, overlay backdrop).
- **Post-click delays**: React state updates + API calls can take 1-3 seconds. Use post_delay: 2.0 for navigation clicks on Khan Academy (higher than the 1.0s default for simpler platforms).

---

## 8. Edge Cases and Quirks

### 8.1 "Up Next" Skips Content

The "Up next" / "Up next for you!" CTA uses a mastery-adaptive algorithm. It may:
- Skip videos/articles the system thinks you don't need
- Jump straight to an exercise in a different lesson
- Recommend a quiz before all exercises are done (if enough skills show progress)

**Rule**: NEVER use "Up next" for sequential course completion. Always navigate via sidebar or skill links.

### 8.2 Learn Items Open as Modals

When clicking a video or article from the unit page, it opens in a modal overlay containing:
- Close button (X) to dismiss
- The content (video player or article text)
- Possible "Up next" link within the modal

After the video/article is complete, close the modal and re-scan the unit page for the next item.

### 8.3 "Start Over" Mechanics

- The "Start over" button only appears after 2 incorrect answers on an exercise, quiz, or unit test
- Using "Start over" resets the exercise -- no mastery points are awarded for the abandoned attempt
- If only 1 question is answered incorrectly, a "Bonus question" opportunity may appear instead

**Rule**: NEVER click "Start over". Continue answering remaining questions.

### 8.4 Mastery Challenges Are Time-Gated

Mastery Challenges unlock when:
- 3+ skills at Familiar level
- 1+ skill at Proficient level
- 12+ hours since last Mastery Challenge

These may appear as navigation suggestions. They are optional and can be skipped for sequential completion.

### 8.5 Course Challenge Scope

The Course Challenge is a 30-question assessment spanning the entire course. Only start it after meaningful progress across all units. It can be retaken.

### 8.6 No Paywalled Content (Mostly)

- Khanmigo (AI tutor) features require a paid subscription or district partnership
- Khan Academy Districts features (Mastery Tower game, admin goals) are partnership-only
- Core learning content (videos, articles, exercises, quizzes, tests) is fully free

**Rule**: Dismiss any Khanmigo prompts. Skip/close AI tutor features.

### 8.7 Video Completion Strictness

- Skipping ANY portion (even 1 second) marks the video incomplete
- Closing the page/tab before the video ends = incomplete
- Fast-forwarding to the end = incomplete
- BUT: Playing at 2x speed and watching the full video = complete

**Rule**: Use video_poll with poll_interval: 30. Let the video play to absolute end.

### 8.8 Exercise Question Counts Vary

- Exercises: 4-7 questions (varies by skill)
- Quizzes: Minimum 5 questions
- Unit Tests: Minimum 9 questions
- Course Challenges: Always 30 questions

### 8.9 Assignment vs. Self-Paced Navigation

- Assignments appear at the top of the left sidebar, sorted by due date
- The blue "Next assignment" button navigates to assignment content, not course-order content
- Assignment content can come from ANY unit in the course (not necessarily the current one)

**Rule**: Ignore "Next assignment" button. Follow curriculum order via sidebar content list.

### 8.10 SPA URL Structure

Khan Academy URLs follow predictable patterns:
- Course: `/science/hs-chemistry`
- Unit: `/science/hs-chemistry/x-unit-name`
- Exercise: `/science/hs-chemistry/x-unit-name/e/exercise-name`
- Video: `/science/hs-chemistry/x-unit-name/v/video-name`
- Article: `/science/hs-chemistry/x-unit-name/a/article-name`

URL changes indicate navigation occurred (useful for validation).

---

## Summary: Key Rules for Spark Claude

1. **Sequential, not mastery-adaptive**: Navigate content in curriculum order (Learn > Practice > Quiz > Unit Test), not via "Up next" which follows mastery algorithm
2. **Videos via video_poll**: Let videos play completely. No skipping. poll_interval: 30
3. **Articles via access**: Open the article, extract content, confirm checkmark
4. **Exercises via Check button**: Answer all questions. Use Check, avoid Skip and Start Over
5. **Modals from unit page**: Learn items open as modals -- close them after completion, then re-scan unit page
6. **Post-delay 2.0s**: React SPA needs more time for state updates than simpler platforms
7. **Sidebar is truth**: The left sidebar shows curriculum-ordered content with completion status -- use it to determine first incomplete item
8. **Completion indicators**: Checkmarks for videos/articles, mastery level colors/text for exercises, percentage for unit/course mastery
9. **Ignore Khanmigo prompts**: Skip/dismiss AI tutor features -- they require paid access
10. **mouse_click strategy**: Khan Academy is a React SPA -- use mouse_click as the default click strategy for all elements

---

## 10. Dropdown/Combobox Interaction (Perplexity Deep Research - Feb 11, 2026)

**Source**: Perplexity Deep Research, Tier 2 escalation after 25 failed Spark attempts

### 10.1 Khan Academy's Dropdown Implementation

Khan Academy uses the **Wonder Blocks** component library with custom ARIA combobox pattern:
- **Trigger**: `<button role="combobox" aria-haspopup="listbox" aria-expanded="false">`
- **Popup**: `<div role="listbox">` containing `<div role="option">` elements
- **Rendering**: Popup uses React Portal (Popper.js) -- renders as a SEPARATE DOM subtree at end of body

### 10.2 Chrome macOS AX Role Mapping

| HTML/ARIA Role | macOS AX Role | Notes |
|---|---|---|
| Native `<select>` | AXPopUpButton | Children become AXMenuItem when open |
| `role="combobox"` on `<button>` | **AXComboBox** | What Khan Academy uses |
| `role="listbox"` | **AXList** | The popup container |
| `role="option"` | **AXStaticText or AXGroup** | Individual selectable items |
| Native `<option>` inside `<select>` | AXMenuItem | Only for native select |

### 10.3 Why Previous Approaches Failed

1. **AXMenuItem search**: Khan Academy uses custom combobox, NOT native `<select>`. AXMenuItem only exists for native HTML select elements.
2. **AXValue setting**: The opener is a `<button>`, not an `<input>`. `AXUIElementSetAttributeValue(kAXValueAttribute)` bypasses React's internal state management entirely. Even if the value appears to change, React's state tree doesn't know about it, so clicking "Check" submits no answer.
3. **discover_menu**: AXMenu doesn't exist for ARIA combobox pattern.

### 10.4 Where Popup Options Appear

- The AXComboBox is nested inside AXTable > AXRow > AXCell > AXGroup > AXGroup
- The popup AXList is **NOT** a child of AXComboBox
- The popup AXList appears as a **separate subtree**, typically under the AXWebArea or at the same level as other top-level page elements
- The AXComboBox's `aria-controls` attribute references the popup's ID

### 10.5 Correct Interaction Patterns

**Pattern A: Keyboard Navigation (Recommended by VoiceOver)**
1. Focus the combobox (AXPress action or click)
2. The popup opens, `aria-expanded` becomes true
3. Down Arrow moves focus to first option (via `aria-activedescendant`)
4. Additional Down Arrows cycle through options
5. Enter or Space selects the currently active option
6. The popup closes, `aria-expanded` returns to false, and React state updates

**Pattern B: Find and Click Popup Option (Used by Taey-Ed)**
1. Click the AXComboBox to open popup
2. Wait 500-700ms for React Portal to render
3. Search the entire AX web area for elements matching the answer text (AXStaticText/AXGroup in the popup AXList)
4. Mouse-click the matching option at its reported position
5. The dropdown closes and React state updates

### 10.6 Key Element Roles to Search For

| What you're looking for | AX Role | Search scope |
|---|---|---|
| Dropdown trigger | AXComboBox | Within AXTable cells |
| Popup container | AXList | Top-level under AXWebArea |
| Individual options | AXStaticText or AXGroup | Children of the AXList |
| Currently selected indicator | Check for AXSelected=true | On option elements |

### 10.7 Edge Cases

1. **Duplicate AXComboBox elements**: KA renders both row-based and column-based table views. DFS traversal finds row-based first, which is correct.
2. **React Portal rendering delay**: The popup takes time to mount. A 500-700ms delay is recommended.
3. **Option index mapping**: The first option in the listbox is index 0, but the combobox may have a placeholder "Select an answer" as a disabled option at index 0.
4. **aria-activedescendant**: After opening with a click, the first arrow key press moves activedescendant to the first option.
5. **After selection**: The AXComboBox description/value changes from "Select an answer" to the selected option text, so the next `find_and_click("Select an answer", AXComboBox)` correctly targets the next unsolved dropdown.


---

## Perplexity Deep Research Update (2026-02-12 21:53)

```yaml
# ============================================================
# PLATFORM KNOWLEDGE: Khan Academy Combobox / Dropdown
# Source: Perplexity Deep Research, February 12, 2026
# ============================================================

khanacademy_combobox_architecture:
  component: "@khanacademy/wonder-blocks-dropdown"
  version: "10.8.0+"
  pattern: "WAI-ARIA 1.2 Select-Only Combobox"
  trigger_aria: 
    role: "combobox"
    aria-haspopup: "listbox"
    aria-expanded: "false|true"
    aria-controls: "listbox-id"
    aria-activedescendant: "option-id"
  popup_aria:
    role: "listbox"
    children_role: "option"
    children_have: "aria-selected"
  rendering: "React Portal to document.body (Popper.js positioning)"
  popup_location: "Top-level under AXWebArea, NOT child of AXComboBox"

khanacademy_combobox_ax_mapping:
  trigger:
    ax_role: AXComboBox
    name: "Select an answer"  # before selection
    name_after: "<selected value>"  # after selection
  popup:
    ax_role: AXList
    subrole: null
    location: "top-level child of AXWebArea"
    appears_after: "500-700ms delay (React Portal mount)"
  options:
    ax_role: AXGroup  # per Core AAM: role=option → AXGroup
    # May also appear as AXStaticText for simple text-only options
    parent: AXList (the popup)
    has_aria_selected: true

khanacademy_combobox_WRONG_approaches:
  - approach: "discover_menu / AXMenuItem"
    why_fails: "AXMenu/AXMenuItem only exist for native <select>. ARIA combobox produces AXList/AXGroup."
  - approach: "find_and_type / set AXValue"  
    why_fails: "Trigger is <button> not <input>. AXValue bypass doesn't update React state."
  - approach: "find_and_click AXMenuItem"
    why_fails: "AXMenuItem elements don't exist for ARIA combobox pattern."
  - approach: "Search only combobox children for options"
    why_fails: "Popup is rendered via Portal - it's a sibling of, not child of, the combobox in DOM/AX tree."

khanacademy_combobox_CORRECT_interaction:
  recommended_pattern: "keyboard"
  steps:
    - action: "mouseclick AXComboBox (name='Select an answer')"
    - action: "wait 700ms"
    - action: "send Down Arrow key × N (N = option position, 1-indexed)"
    - action: "send Enter key"
    - action: "wait 500ms" 
    - action: "repeat for next AXComboBox"
  alternative_pattern: "find-and-click"
  alt_steps:
    - action: "mouseclick AXComboBox"
    - action: "wait 700ms"
    - action: "search ENTIRE AXWebArea for answer text (AXStaticText/AXGroup in AXList)"
    - action: "mouseclick matching option"
    - action: "wait 500ms"
  post_selection:
    - "AXComboBox name changes from 'Select an answer' to selected value"
    - "Next find for 'Select an answer' skips already-answered dropdowns"

khanacademy_combobox_edge_cases:
  duplicate_comboboxes: "KA renders row-based AND column-based table views. May see 6 AXComboBox (3 visible + 3 hidden). DFS finds row-based first (correct)."
  portal_render_delay: "500-700ms minimum. Use 700ms+ to be safe."
  option_count: "Typically 3 options for charge exercises (e.g., +1, -1, 0 or Positive, Negative, Neutral)"
  aria_activedescendant: "After opening, first Down Arrow sets activedescendant to first option"
  disambiguation: "Answer text may appear both in table labels AND popup options. Use AXList parent context to disambiguate."

core_aam_role_mapping:
  note: "W3C Core AAM 1.1/1.2 defines ARIA→macOS AX mappings"
  combobox: AXComboBox
  listbox: AXList
  option: AXGroup  # NOT AXMenuItem!
  menu: AXMenu
  menuitem: AXMenuItem
  # AXMenuItem is ONLY for role=menuitem or native <option> in <select>
  # role=option always maps to AXGroup on macOS
```

### Recommended YAML Update for EXERCISE_DROPDOWN

The current YAML uses `find_and_click` for the option text with no role filter, which is fragile. The recommended update uses keyboard navigation:

```yaml
EXERCISE_DROPDOWN:
  description: >
    ASSESSMENT - Exercise with dropdown/combobox table.
    Extracts question, asks LLM for ALL dropdown values in one call,
    fills each ComboBox via keyboard navigation, clicks Check.
  markers:
    - text: "Select an answer"
    - text: "Check"
  extract:
    scope: webarea
    question:
      - role: AXStaticText
        contains: "?"
        text: true
      - role: AXStaticText
        parentrole: AXCell
  tree: 
    type: sequence
    children:
      # Step 1: Extract question
      - type: action
        action: extractquestion
        store: qdata
      
      # Step 2: Ask LLM for ALL dropdown values at once
      - type: action
        action: sendtollm
        params:
          question: >
            {qdata.questiontext}
            Read the table. For each row's dropdown (top to bottom),
            what value should be selected? 
            Answer as a comma-separated list of EXACT option values.
            Example: Positive,Negative,Neutral
          questiontype: solve
          context: "{qdata.referencetexts}"
        store: all_answers
      
      # Step 3-5: Fill dropdowns via keyboard
      # For each dropdown: click combobox, Down Arrow × N, Enter
      - type: action
        action: findandclick
        params:
          target: "Select an answer"
          role: AXComboBox
          strategy: mouseclick
          matchmode: exact
      - type: action
        action: wait
        params:
          seconds: 0.7
      - type: action
        action: sendkeys
        params:
          keys: "{computed_arrow_sequence_1}"  # Down Arrow × position + Enter
      - type: action
        action: wait
        params:
          seconds: 0.5
      
      # Repeat for dropdown 2 and 3...
      
      # Final: Click Check
      - type: action
        action: findandclick
        params:
          target: "Check"
          role: AXButton
          strategy: mouseclick
          matchmode: exact
```

**Note on `sendkeys` handler**: If the `sendkeys` action handler doesn't exist yet, the alternative is to use the find-and-click pattern but search the **entire AXWebArea** scope (not just combobox children) with a 700ms delay, and filter for elements whose parent is AXList.

---

## Dropdown Combobox Keyboard Navigation (Feb 12, 2026 - Perplexity Deep Research)

**Problem**: Khan Academy uses Wonder Blocks select-only ARIA 1.2 comboboxes. The dropdown popup renders via React Portal, creating a separate DOM subtree. Three interaction strategies were tested:

### Failed Strategies

1. **CGEventCreateMouseEvent (mouse_click)**: Creates macOS-level synthetic mouse events. The browser correctly identifies the DOM element at the click coordinates, BUT React's synthetic event system does NOT process CGEvent mouse events through its event delegation for Portal-rendered elements. The tree reports success (click happened) but React state doesn't update - dropdowns remain empty.

2. **AXPress (ax_press)**: Chrome on macOS does NOT reliably support the `kAXPressAction` accessibility action for web `role="option"` elements. Returns success but no effect.

### Working Strategy: Keyboard Navigation

**ARIA 1.2 Select-Only Combobox Keyboard Interaction:**
- When popup is open, first option is visually focused (via `aria-activedescendant`)
- **Down Arrow**: Moves visual focus to next option
- **Up Arrow**: Moves visual focus to previous option
- **Enter/Return**: Selects the visually focused option, closes popup
- **Escape**: Closes popup without selecting
- **Type-ahead**: Typing a printable character jumps to matching option (first char match, cycles on repeat)

**Why keyboard works when mouse doesn't:**
- Keyboard events go through Chrome's DOM event system
- React captures keyboard events at document level via `addEventListener`
- This works for ALL React elements including Portal-rendered ones
- The keyboard event pipeline is: CGEventCreateKeyboardEvent → Chrome window → DOM focused element → document bubble → React synthetic event

**Implementation Pattern:**
1. Click AXComboBox to open popup (mouse_click works on the trigger - it's NOT a portal element)
2. Wait 0.7s for popup animation
3. Press Down arrow N-1 times to reach option at position N (first option highlighted by default)
4. Press Return to select
5. Wait 0.5s for React state update

**Post-selection verification:** AXComboBox `name` attribute changes from "Select an answer" to the selected value text.

### BT Implementation (lookup_match + for_each)

Since the BT engine doesn't support dynamic loops, the keyboard navigation uses:
1. `find_all AXMenuItem` to discover options in order
2. `send_to_llm` with options in context, asking for position number (1-indexed)
3. `lookup_match` to map position → key sequence: `{"1": ["return"], "2": ["down", "return"], ...}`
4. `for_each` over key sequence to execute `press_key` for each key

---

## UNIT_OVERVIEW Navigation Fix (Feb 14, 2026 - Escalation Fix)

### Problem
UNIT_OVERVIEW behavior tree uses `find_all(AXLink)` + `send_to_llm(navigate)` to pick the next content item. The page has **113 AXLinks** including navigation, breadcrumbs, sidebar units, footer links, and actual content items. The Ollama LLM (even with detailed prompts) repeatedly picks wrong links:
- "Skip to main content" (nav link at depth 14)
- "Unit 1: Atoms, isotopes, and ions" (self-referencing → same_screen loop)
- "Unit guides" (navigation link)

### Root Cause
Two compounding issues:
1. **Prompt too complex for small LLM**: A 20-line prompt explaining what to ignore and what to click was unreliable with 113 items
2. **Weaviate stale trees**: V10 vector store was seeded with old trees. Vector matching (primary path) served stale trees even after YAML was updated

### Tree Structure (from accessibility tree analysis)
Content-level links are at depth 30-31:
- depth 30: `completed Video/Article [title] (Opens a modal)` = DONE
- depth 31: `Understand: [topic]` / `Apply: [topic]` = exercise skill links
- depth 31: `Try again` = previously attempted exercise (FIRST PRIORITY)
- depth 31: `Practice` = unattempted exercise (SECOND PRIORITY)

Navigation/noise links are at depth 14-25 (skip, sidebar units, breadcrumbs, footer).

### Fix
Ultra-simplified LLM prompt (6 lines instead of 20):
```
Return "Try again" if it appears in the list.
Otherwise return "Practice" if it appears.
Otherwise return the first link containing "Video" or "Article"
that does NOT start with "completed".
Return ONLY the exact text. Never return "Skip to main content" or any "UNIT" link.
```

This reduces the LLM task from "understand page structure and completion indicators" to "find these exact strings in a list" - trivially simple even for 8B models.

### Key Lesson
When the LLM has to filter a large list (100+ items), keep the prompt as simple as possible. Prefer exact string matching instructions over complex reasoning about page structure. The page structure knowledge should inform the YAML design, not the runtime LLM prompt.

### Vector Store Maintenance
After updating YAML trees, **must also update Weaviate entries** since V10 vector matching is the primary path (before YAML fallback). Delete old entries and re-store with current tree.

---

## Appendix C: Matching/Sorting Exercise Details (Grok Research, Feb 17 2026)

**Source**: Grok (LOGOS), Tier 2 escalation research

### Perseus Matcher Widget (Drag-and-Drop)

Khan Academy uses **Perseus** (open-source exercise framework) for matching/sortable exercises. These are drag-and-drop matchers (e.g., subatomic particles to charges) implemented as sortable lists.

**ARIA Pattern**:
- Container: `role="listbox"` or `role="group"` with `aria-label="Sortable list"`
- Items: `role="option"` or `role="listitem"`, with `aria-grabbed="true/false"` for drag state
- Modern implementation: Custom keyboard handlers + `aria-roledescription="sortable item"`
- No native HTML5 draggable - custom React (@dnd-kit or internal) with keyboard support

**Keyboard Accessibility**:
- Tab to list → Space/Enter to "grab"
- Arrow keys to reorder (up/down or left/right)
- Space/Enter to "drop"
- Screen readers announce "grabbed" and position changes

### Why Automation Fails on Matching Exercises

1. **No standard AX controls**: No AXRadioButton or AXCheckBox - items are custom sortable groups
2. **React overlay interference**: Buttons under React overlay or modal shadow - mouse_click hits invisible layer
3. **State-dependent buttons**: "Next question" may need page reflow or JS event (not pure AXPress trigger)
4. **React synthetic events**: Accessibility clicks fail if element not focused or if React synthetic events not fired

### Progress Buttons After Hints Exhausted

| Button | Role | Reliability |
|--------|------|-------------|
| **"Next question"** | AXButton | State-dependent, sometimes fails automation |
| **"Show solution and move on"** | AXLink | More reliable fallback after hints exhausted |

**Recommendation**: After hints exhausted, prefer "Show solution and move on" (AXLink) as it has a direct JS handler to reveal solution + advance, bypassing validation issues.

### Key Insight
Matching exercises are currently **unsolvable** with the 16 registered handlers (no drag-and-drop support). The correct strategy when encountering them is:
1. Use hints to exhaust all hints
2. Click "Show solution and move on" or "Next question" to advance
3. Accept incorrect mark and move to next question
