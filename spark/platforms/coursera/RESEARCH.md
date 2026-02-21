# Coursera -- Platform Navigation Research

**Source**: Platform documentation and prior automation experience
**Purpose**: Navigation rules for Taey-Ed screen mapping and BT generation

---

## 1. Platform Overview

Coursera is an online learning platform with structured courses from universities and companies. Content is organized hierarchically:

**Course > Week/Module > Lesson > Content Items**

### Content Types

| Type | Description | Completion |
|------|-------------|------------|
| **Video** | Lecture videos (5-20 min) | Must finish watching (progress tracked) |
| **Reading** | Text-based content | Scroll through or mark complete |
| **Quiz** | Graded assessment | Answer all questions, submit |
| **Practice Quiz** | Ungraded practice | Answer questions, can retry |
| **Programming Assignment** | Code submission | Submit code, auto-graded |
| **Peer-Graded Assignment** | Written submission | Submit + review peers |
| **Discussion Prompt** | Forum post required | Post to discussion |

Coursera has both free (audit) and paid (enrolled) modes. Audit mode limits quiz access and certificates.

---

## 2. Navigation Structure

### 2.1 Course Home Page

The course home page shows:
- Course title and instructor info
- Progress bar (% complete)
- Weekly modules listed vertically
- Each week shows: title, description, estimated time, completion status
- "Start" or "Resume" button for current position

### 2.2 Week/Module View

Clicking a week expands or navigates to show all items in order:
- Items listed sequentially: Video → Reading → Quiz → etc.
- Each item shows: type icon, title, duration/length, completion checkmark
- Items are in a left sidebar or main content area depending on view

### 2.3 Content Navigation

| Element | Location | Behavior |
|---------|----------|----------|
| **"Next"** | Bottom-right of content | Advance to next item in sequence |
| **"Previous"** | Bottom-left of content | Go back to previous item |
| **Sidebar items** | Left panel | Click to jump to any item in current week |
| **Week selector** | Left panel top | Switch between weeks |
| **"Mark as completed"** | Reading pages | Mark reading as done |
| **"Submit"** | Quiz pages | Submit quiz answers |

### 2.4 Video Player

- Standard HTML5 video player
- Play/pause button
- Progress bar with scrubbing
- Speed control (0.75x, 1x, 1.25x, 1.5x, 2x)
- Full-screen toggle
- Transcript panel (expandable, synced to video)
- Download options (video, transcript)
- "Next" button appears when video completes or is always visible

### 2.5 Quiz Interface

- Questions displayed one at a time or all at once (depends on course)
- Question types: multiple choice (radio), multiple select (checkbox), text input, numeric
- "Check" or "Submit" button for each question or entire quiz
- Feedback shown after submission
- Retry allowed on practice quizzes (unlimited), graded quizzes have attempt limits
- "Submit Quiz" at bottom when all questions answered

---

## 3. Completion / Progress Model

### 3.1 Item Completion

| Content | Completion Trigger |
|---------|-------------------|
| Video | Watch to end (or past threshold ~80%) |
| Reading | Click "Mark as completed" or scroll to end |
| Quiz | Submit answers (pass/fail tracked separately) |
| Practice Quiz | Submit (no grade requirement) |
| Assignment | Submit work |

### 3.2 Week Completion

A week is complete when all required items show checkmarks. Optional items (ungraded) may not be required.

### 3.3 Course Completion

All weeks with required items completed → certificate available (if enrolled).

---

## 4. Common Screen Patterns

### 4.1 Video Screen

**Signals**: Video player element, play/pause controls, transcript section, progress bar
**Action**: Click play, wait for video to finish, click "Next"
**Key elements**:
- Play button (usually centered overlay or bottom-left control)
- "Next" button (bottom-right, may appear after video ends)
- Video progress indicator

### 4.2 Reading Screen

**Signals**: Long scrollable text content, "Mark as completed" button, reading time estimate
**Action**: Scroll to bottom or click "Mark as completed", then click "Next"
**Key elements**:
- Article text content
- "Mark as completed" button
- "Next" button

### 4.3 Quiz Screen

**Signals**: Radio buttons, checkboxes, text inputs, "Submit" button, question numbering
**Action**: Select answers, click "Submit Quiz"
**Key elements**:
- Question text
- Answer options (radio/checkbox)
- "Submit Quiz" or "Submit" button
- Point values shown per question

### 4.4 Navigation/Course Overview Screen

**Signals**: Many links, week/module listing, progress indicators, no active content
**Action**: Click "Resume" or find next incomplete item
**Key elements**:
- "Resume" or "Start" button
- Week/module links
- Completion checkmarks

### 4.5 Transition Screen

**Signals**: "Congratulations" or completion message, "Next" button prominent
**Action**: Click "Next" to advance
**Key elements**:
- Success/completion message
- "Next" or "Continue" button

---

## 5. Accessibility Tree Patterns

### 5.1 Common Roles

| AT-SPI Role | Coursera Usage |
|-------------|----------------|
| `AXButton` | Play, Next, Submit, Mark as completed |
| `AXLink` | Navigation items, sidebar links, week links |
| `AXRadioButton` | Multiple choice quiz answers |
| `AXCheckBox` | Multiple select quiz answers |
| `AXStaticText` | Content text, question text, labels |
| `AXTextField` | Text input quiz answers |
| `AXProgressIndicator` | Video progress, course progress |
| `AXGroup` | Content sections, quiz question groups |
| `AXHeading` | Section titles, question titles |

### 5.2 Key Button Names (AT-SPI)

- **"Next"** or **"Go to next item"** — advance to next content
- **"Previous"** — go back
- **"Submit Quiz"** or **"Submit"** — submit answers
- **"Mark as completed"** — complete reading
- **"Play"** / **"Pause"** — video controls
- **"Resume"** — return to last position in course

---

## 6. Platform-Specific Notes

### 6.1 Login State

Coursera uses Google/Apple/email login. The automation assumes already logged in with an active session in the browser.

### 6.2 Popups and Modals

- **Cookie consent**: May appear on first visit
- **Upgrade prompts**: "Upgrade to get graded assignments" — dismiss or ignore
- **Survey popups**: "How's your experience?" — close/dismiss
- **Certificate prompts**: After course completion

### 6.3 Auto-play

Videos do NOT auto-play on Coursera. Must click play explicitly.

### 6.4 Simpler Than Khan Academy

- Linear progression (no mastery-based reordering)
- No "Up next" adaptive routing
- No mastery levels per skill
- Sequential: item 1 → item 2 → item 3
- "Next" button is the primary navigation mechanism
