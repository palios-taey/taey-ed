# Acellus Platform Research
## `spark/platforms/acellus/RESEARCH.md`

*Generated: February 16, 2026 — Taey-Ed V7 Platform Research (Perplexity Deep Research)*

***

## 1. Platform Overview

Acellus is a **video-first, self-paced online learning platform** for grades K–12 developed by the International Academy of Science. Every lesson teaches a single concept through a short HD video, followed immediately by practice problems to assess understanding. Students move at their own pace — once they demonstrate mastery of a concept (sufficient correct answers), the system advances them to the next lesson.[^1][^2][^3][^4][^5]

### Course Organization

Courses are organized hierarchically:

- **Course** → contains multiple **Units**
- **Unit** → contains multiple **Lessons** (video + practice problems), ending with a **Review** and a **Unit Exam**[^6]
- **Lesson** = one video + one set of practice problems (this is the atomic "step")[^7]
- **Review** = end-of-unit review where students can rewatch videos and do practice problems before the exam[^6]
- **Exam** = unit-level assessment; scored, limited retakes[^8][^6]

### Content Types (in order of encounter within a unit)

1. **Video lesson** — short, focused on one concept; taught by a teacher in HD[^9]
2. **Practice problems** — multiple problems after each video assessing that concept[^4]
3. **Unit review** — pre-exam review with access to all unit videos and practice problems[^6]
4. **Unit exam** — formal assessment; triggers Exam Recovery on failure[^8]
5. **Special Lessons** — supplemental lessons inserted by the system (appear as a "bubble" above the briefcase icon)[^3]
6. **Vectored Instruction** — remedial lessons from lower grade levels, triggered by Prism Diagnostics when foundational gaps are detected[^10][^1]
7. **Writing assignments** — delivered via "Writing Tutor" with grammar/spelling checking[^6]

### Acellus Academy vs. School-Based Acellus vs. Power Homeschool

| Feature | Acellus Academy | Acellus for Schools | Power Homeschool |
|---|---|---|---|
| Type | Accredited K-12 online school [^11] | Institutional license for schools [^1] | Parent-managed homeschool [^12] |
| Interface | Acellus Gold Edition [^13] | Acellus 7 HD or Gold [^7] | Acellus Gold [^12] |
| Teacher | Acellus Academy staff | School's own teachers [^1] | Parent is supervisor [^12] |
| Enrollment | Year-round, self-paced [^11] | School schedule | Flexible [^12] |
| Goal setting | Parent via portal [^14] | Teacher via admin [^15] | Parent via portal [^12] |
| Diploma | Yes (accredited) [^11] | Through school | No |

### Course Modes (teacher/parent configurable)

- **Default Mode** — standard course version[^16]
- **Honors Mode** — more comprehensive lectures, increased rigor[^16]
- **Tuned-Learning (TL) Mode** — accelerated progress, emphasis on most crucial concepts; effective for special education[^16]
- **Credit Recovery Mode** — high school only, assesses only most-critical lectures, 40-step weekly limit[^16]

Students are **never notified** when course mode changes.[^16]

***

## 2. Navigation Structure

### Login → Course Page Flow

1. Student navigates to `signin.acellus.com` and enters credentials[^17]
2. **First-time login**: Orientation video plays (specific to the interface — elementary, middle, or high school)[^7]
3. After orientation, student arrives at the **Course Page** (the dashboard)[^7]
4. Course Page displays all enrolled courses as **Course Tiles**[^7]

### Course Page (Dashboard)

- Shows a **list of Course Tiles** — one per enrolled course (e.g., Social Studies, Math, Science, Language Arts)[^7]
- Each **Course Tile** displays:[^18]
  - Course name
  - Projected completion date
  - **Goal Status Indicator** — M–F dots showing daily goal completion status[^7]
  - Course progress percentage
  - Overall grade
- **Clicking on a course tile** resumes from where the student last left off — no separate "Start" or "Resume" button per tile; the tile itself is the click target[^6][^7]

### Inside a Course

Once a course is selected, the student is taken directly to their **current assignment** (the next uncompleted step). The flow inside a course is strictly sequential:[^6]

```
Video Lesson → Practice Problems → Video Lesson → Practice Problems → ... → Unit Review → Unit Exam → [next unit]
```

**Top-of-screen progress bar**: Shows how much work remains in the current step. Hover to see remaining problems/sections.[^7]

**Menu (☰ hamburger icon)** — top-right corner, contains:[^6][^7]
- **Class Grade** — current grade in the course
- **Goal Progress** — weekly goal status (M-F checkmarks)
- **Lesson List** — scrollable list of all lessons; allows rewatch video (video icon) or retake lesson (pencil icon)[^19][^13]
- **Textbook** — written version of the concept covered in the video[^7]
- **Help Videos** — additional resource videos on the current concept[^7]
- **Ask a Question** — connects student to an active teacher monitoring the class[^7]

### Bottom Navigation Bar (Acellus Gold Edition)

The bottom of the screen has navigation tabs. These are **non-educational features** and should generally be avoided by automation:[^6]

| Tab | Function | Automation Relevance |
|---|---|---|
| **Courses** (Home) | Return to Course Page / dashboard | YES — use to switch courses |
| **Activities** | Learning activities (math facts, coding, science live, coloring, etc.) | NO — locked until daily goals met; purely enrichment [^13] |
| **Library** | Read books written/published by Acellus students | NO — not course content [^6] |
| **Classmates** | Follow other students, post achievements, create competitions | NO — social feature only [^6] |
| **Stats** | Gold earned, time spent, course progress, GPA | READ-ONLY — useful for progress checking [^6] |
| **Store** | Redeem gold credits for prizes | NO — not educational [^13] |

***

## 3. Completion / Progress Model

### Steps — The Atomic Unit

Everything in Acellus is measured in **"steps"**. A step can be:[^12][^20]
- A video lesson
- A set of practice problems
- A unit review
- A unit exam
- A Vectored Instruction lesson (supplemental)
- A special lesson

### Goal System

- **Weekly goal** = number of steps per week (set by parent/teacher)[^15][^12]
- Automatically divided into **5-day daily goals** (e.g., 20 steps/week = 4 steps/day)[^12]
- Students can work ahead — completing multiple days' goals in one session[^12]

### Goal Status Indicators (on Course Tiles — M-F dots)

| Indicator | Meaning |
|---|---|
| **Blank (empty circle)** | Daily goal not yet completed [^12] |
| **Checkmark** | Daily goal completed (but not necessarily on that specific day) [^12] |
| **Highlighted Checkmark** | Daily goal completed on that specific day of the week [^12] |

Checkmarks fill sequentially Monday→Friday regardless of which day work was done.[^12]

### Progress Bar (in-course, top of screen)

- Horizontal bar at the top of the screen during a lesson[^7]
- Hover to see how many problems/sections remain in the current step[^7]
- Separate from the **Course Progress** percentage (overall % of course completed)

### Course Progress

- Viewable via the **Stats** tab: shows a bar from 0–100%[^21]
- Tap the progress bar to see exact count: "X steps completed out of Y total steps"[^21]
- Also visible on each Course Tile in the parent portal as a percentage[^22]
- **Projected Completion Date** shown on each Course Tile[^18]

### Gold Credits

- Earned by completing steps correctly[^13]
- Amount correlated to assignment difficulty and mastery demonstrated[^13]
- Tracked on Stats page with visual gold depiction[^23]
- Redeemable in the Acellus Gold Store for prizes[^13]
- NOT a progress indicator — purely motivational/reward[^13]

### Grading

- Each practice problem set and exam is scored[^3]
- Overall course grade visible via ☰ menu or parent portal[^7]
- Exam passing threshold: approximately 70–75%[^3]
- Retaking an exam replaces the original grade[^19]

***

## 4. Content Ordering Rules

### Strictly Linear Progression

Acellus courses are **strictly linear**. Students cannot:[^5][^4]
- Skip ahead to a future lesson
- Jump to a specific unit
- Choose which lesson to do next

The system always presents the next uncompleted step. Clicking a course tile takes the student directly to their current position.[^7]

### Lesson Flow

For each lesson within a unit:
1. **Video plays first** — cannot be fast-forwarded on first viewing[^3]
2. Can be paused, and skipped back in 15-second increments[^24][^6]
3. **Save Video Position** — if student logs out mid-video, position is saved and resumed next login[^24]
4. On **subsequent viewings** (via Lesson List), advancing/skipping is allowed[^3]
5. After video completes → **Practice problems** appear automatically[^2][^4]
6. If mastery demonstrated (enough correct answers) → advance to next lesson[^2]
7. If mastery NOT demonstrated → more practice, help videos, or Vectored Instruction may trigger[^25]

### Unit Flow

1. All lessons in unit completed → **Unit Review**[^6]
2. Review: can rewatch any video from the unit + practice problems; all help resources available[^6]
3. After review → **Unit Exam**[^6]
4. Exam completed (pass or exhaust retries) → next unit begins[^8]

### Intervention Insertions (break the linear flow)

- **Vectored Instruction**: system inserts remedial lessons from lower grade levels when foundational gaps detected. These steps count toward daily goals but do NOT reduce total course step count. A label appears at top of screen when VI is active.[^20][^10]
- **Special Lessons**: appear as a "bubble" above the briefcase icon; must be completed when assigned[^3]

***

## 5. Buttons and CTAs

### Primary Action Buttons (in-course)

| Button/CTA | When Visible | What It Does | Safe to Click? |
|---|---|---|---|
| **Course Tile** (click anywhere on tile) | Course Page | Opens course at current position [^7] | YES — always safe |
| **Play / 15s Rewind** | During video lesson | Play/pause video; rewind 15 seconds [^6][^24] | YES — safe |
| **(No explicit "Next" during video)** | Video playing | Video auto-advances to problems when complete [^2] | N/A — wait for completion |
| **Submit** (for practice problems) | After answering practice problems | Submits answers for scoring [^6] | YES — only after answering |
| **Continue** (after exam) | Exam results screen | Accepts current score, moves forward in course [^26] | YES — but check if "Try Again" is preferred |
| **Try Again for Extra Credit** | Exam results screen (if passed) | Re-enters Exam Recovery to review mistakes and retake [^26] | CAUTION — only if student wants higher score |
| **Check** (writing assignments) | Writing Tutor | Submits writing for grammar/spelling check [^6] | YES — after writing |
| **Submit** (writing assignments) | After checking writing | Final submission of writing assignment [^6] | YES — after corrections |

### Help/Resource Buttons (via ☰ menu)

| Button | What It Does |
|---|---|
| **Lesson List** | View all lessons, rewatch videos, retake assessments [^7][^19] |
| **Textbook** | Written version of current lesson concept [^7] |
| **Help Videos** | Alternative teaching videos for current concept [^7] |
| **Ask a Question** | Connect to live teacher monitor [^7] |

### Navigation Buttons

| Button | What It Does | Safe to Click? |
|---|---|---|
| **Back arrow** (top-left in some views) | Return to previous screen / Course Page | YES — but may interrupt current step |
| **☰ (hamburger menu)** | Opens resource panel | YES — always safe, just overlay |
| **Bottom nav tabs** (Activities, Library, etc.) | Navigate to non-course features | AVOID during automation — not educational [^6] |

### Completion/Goal Messages

- When daily goal completed in all courses → "Goal Complete" message appears[^12]
- Activities become unlocked[^13]
- Student can continue working or switch to activities

***

## 6. Progress-Aware Navigation Guidance

### Choosing Which Course to Enter

On the Course Page, multiple Course Tiles are visible. For automation:

1. **Check Goal Status Indicators** on each tile — blank circles indicate incomplete daily goals[^12]
2. **Prioritize courses with blank indicators** — these have not met today's goal
3. **Within unfinished courses**, prefer the one with the **lowest course progress percentage** (needs the most work)[^18]
4. **If all daily goals met**: either continue working for extra credit or stop (Activities unlock)[^13]

### In-Course Navigation Strategy

- Acellus auto-positions to the current assignment  — no need to manually navigate to a lesson[^6][^7]
- Simply click the Course Tile and the platform drops you at the right place
- **Do NOT** use the Lesson List to manually select lessons — this is for rewatch/retake only[^19]
- **Do NOT** click Activities, Library, Store, or Classmates during automation — these are non-educational features[^6]

### Bottom Nav Bar Safety Rules

| Tab | Action for Automation |
|---|---|
| **Courses/Home** | SAFE — returns to Course Page to switch courses |
| **Activities** | AVOID — enrichment only, locked until goals met |
| **Library** | AVOID — student-written books, not coursework |
| **Classmates** | AVOID — social features only |
| **Stats** | READ-ONLY OK — useful to check course progress % |
| **Store** | AVOID — reward redemption, not educational |

### Daily Goal Completion Detection

When a student completes their daily goal in a course:
- A confirmation message appears encouraging them to move to the next subject[^12]
- The goal indicator changes from blank to checkmark[^12]
- This is a signal to **switch to the next course** (return to Course Page, select next tile with blank indicator)

***

## 7. Accessibility / Tree Expectations

### Application Structure

Acellus renders as a **web application** inside a browser or the Acellus native app :

```
AXApplication "Acellus" (or browser name)
  └── AXWindow
        └── AXWebArea "Acellus | Student"
              └── [all course UI elements]
```

### Known AX Roles in Acellus UI

Based on Taey-Ed's previous automation runs (34 consecutive screens automated) and the platform's web-based architecture :

| UI Element | Expected AX Role | Notes |
|---|---|---|
| Course Tiles | AXLink or AXButton | Clickable cards on Course Page |
| Video player controls | AXButton (Play, Pause, Rewind) | Within video player container |
| Practice problem answers | AXRadioButton (MCQ), AXCheckBox (multi-select), AXTextField (fill-in-blank), AXPopUpButton (dropdown) | Varies by question type |
| Submit/Continue buttons | AXButton | Primary CTAs |
| Hamburger menu (☰) | AXButton | Opens resource overlay |
| Menu items (Lesson List, etc.) | AXLink or AXMenuItem | Within menu overlay |
| Progress bar | AXGroup or AXProgressIndicator | Top of screen |
| Bottom nav tabs | AXLink or AXButton | Activities, Library, etc. |
| Static text / labels | AXStaticText | Lesson content, question text |
| Goal indicator dots | AXGroup containing AXImage or AXStaticText | M-F dots on course tiles |

### JavaFX / WebView Quirks

Acellus has been noted to use **JavaFX** in its native app :
- **Old accessibility elements persist** in the tree after screen transitions (JavaFX layering issue)
- **Must compare tree hashes AND screenshots** to confirm transitions — don't rely on element absence alone 
- Fill-blank and essay screens require multi-step validation 

### Screen Reader Support

Acellus has **limited native screen reader support**:[^27]
- JAWS can navigate basic page structure (buttons, links, text)[^27]
- JAWS OCR needed for images and maps (not natively described)[^27]
- iPad Spoken Content / Speech Controller works for reading on-screen text[^28]
- **Video content is NOT accessible** to screen readers — it's actual video, not text
- Problem text is generally in the accessibility tree as AXStaticText 
- Images in lessons may not have alt text — require Gemini VLM extraction 

### ARIA Expectations

As a web app, Acellus likely uses standard HTML form elements that map to AX roles natively:
- Radio buttons → AXRadioButton
- Checkboxes → AXCheckBox
- Dropdowns → AXPopUpButton (native select) or AXComboBox (custom React)
- Text inputs → AXTextField or AXTextArea
- Buttons → AXButton
- Links → AXLink

**No evidence of extensive custom ARIA usage** — Acellus appears to rely primarily on native HTML elements rather than custom ARIA widgets.[^27]

***

## 8. Edge Cases and Quirks

### Video Behavior

- **First viewing: cannot fast-forward or skip ahead** — only rewind 15s is available[^3]
- **Subsequent viewings: advancing is allowed**[^3]
- **Save Video Position**: if student exits mid-video, position is saved for next login[^24]
- **Auto-advance**: after video completes, practice problems appear automatically — no explicit "Next" click needed between video and problems[^2]
- **videopoll strategy**: use 30-second polling; when video ends, the accessibility tree changes (problems appear or screen transitions) 

### Exam Recovery System (Multi-Strike)

This is a **5-attempt graduated system**, not a simple 3-strikes:[^8]

1. **Fail 1**: Student reviews missed problems with wrong answers shown → retry entire exam
2. **Fail 2**: Help Videos shown for each missed problem (single-example problem-solving videos) → retry
3. **Fail 3**: Full Vectored Instruction — each missed concept reassigned with video lessons and practice → retry
4. **Fail 4**: Help Videos again → final retry
5. **Fail 5**: System **moves student forward automatically**; score is recorded as-is[^8]

Teachers can intervene at any point to repeat steps or reassign content.[^3]

### "Try Again for Extra Credit" (Passed Exams)

After **passing** an exam, students see two options:[^26]
- **Continue** — moves forward, keeps current score
- **Try Again for Extra Credit** — reviews missed problems, allows full retake for higher score

**CRITICAL**: If student clicks **Continue**, they **cannot go back** to retake that exam. The automation should be aware of this — clicking Continue is irreversible for that exam attempt.[^29]

### Vectored Instruction Quirks

- VI lessons count toward daily goal steps but do NOT reduce total course steps needed[^20]
- This can **extend projected completion dates** unexpectedly[^20]
- A label appears at top of screen indicating "Vectored Instruction" is active[^20]
- VI videos may be from **lower grade levels** — will look different from current course content[^10]
- VI steps will NOT be numbered in the lesson list (they are supplemental)[^10]

### Stuck/Recovery Patterns for Automation

| Situation | Detection | Action |
|---|---|---|
| Video still playing | Same screen structure after 30s poll | Continue polling (videopoll) |
| Practice problems appeared | New elements (AXRadioButton etc.) in tree | Extract question, solve, submit |
| Exam Recovery active | Label text "Exam Recovery" in tree; review problems displayed | Solve recovery problems, wait for exam retry |
| Vectored Instruction active | Label text "Vectored Instruction"; lower-grade content | Complete VI steps normally (video + problems) |
| Daily goal completed message | Goal completion notification in tree | Return to Course Page, select next course |
| Special Lesson bubble | Bubble icon above briefcase | Complete special lesson before continuing |
| "Try Again for Extra Credit" screen | Two buttons: Continue + Try Again | Click Continue (or Try Again if score improvement desired) |
| Writing assignment | Writing Tutor interface with text area | Type response → Check → fix errors → Submit [^6] |

### Course Mode Shifts (Background)

- Teachers/parents can change course modes (Default, Honors, TL, Credit Recovery) at any time **without student notification**[^16]
- This can change the number and type of steps presented
- Acellus may also **recommend** mode changes via the Live Monitor[^16]
- Automation should handle gracefully — more/fewer steps may suddenly appear

### Step Retaking

- Students can retake any previous step via Lesson List[^19]
- Retaking sends student **back** to that step; after completion, they return to their current position[^19]
- Retaking an exam/assessment **replaces** the original grade[^19]
- Automation should NOT initiate retakes unless specifically instructed

### Platform-Specific Technical Notes

- **Acellus app vs. browser**: Acellus has native apps (macOS, Windows, iOS, Android, ChromeOS) and also runs in browsers[^30]
- **JavaFX layering**: native app may have stale elements in accessibility tree after transitions 
- **No ARIA landmarks expected**: Acellus is not known for robust ARIA landmark usage; rely on element roles and text content for navigation[^27]
- **Goal indicators as images**: M-F dots may render as AXImage elements rather than text — need visual/structural identification[^7]
- **Orientation video**: first-time login shows an orientation video that must complete before reaching Course Page[^7]
- **Activities lock**: Learning Activities are locked until all daily course goals are met  — attempting to click Activities before goals are complete will show locked state[^13]

***

## Automation Decision Tree Summary

```
[Course Page]
  ├── Any course tile with blank goal

---

## References

1. [Acellus Learning Accelerator — Online Courses for Schools K-12](https://www.acellus.com/schools/) - Elevate the academic achievement of your students by using Acellus Gold. Available for special educa...

2. [The Acellus Student Experience - YouTube](https://www.youtube.com/watch?v=tRCRo8h1zsw) - A look at the student interface in the Acellus Learning System. www.Acellus.com.

3. [[PDF] Acellus Teacher Guide](https://resources.finalsite.net/images/v1709008240/phsd144net/kvjzptezk0ueyu5og8yy/acellusteacherguide.pdf) - You can have students repeat video lessons, practice problems, or both. *** Student will return to p...

4. [[PDF] Acellus - International Academy of Science](https://www.science.edu/Acellus/AcellusBrochure.pdf) - The Acellus Teacher Interface gives teachers the ability to track progress, identify problems, and p...

5. [FAQ | Acellus Academy](https://www.acellusacademy.com/frequently-asked-questions/) - All of our course content is delivered online. There is no need to purchase additional textbooks. Pl...

6. [How to Navigate the Student Interface: Acellus Gold - YouTube](https://www.youtube.com/watch?v=6sXj4VPViRA) - Explore the tailored learning experience of Acellus Gold Edition, with separate interfaces designed ...

7. [[PDF] Student Interface-Acellus 7 HD_v241126](https://www.acellus.com/schools/wp-content/uploads/2024/11/Student-Interface-Acellus-7-HD_v241126.pdf) - After the introduction video, students will be directed to the 'Course Page', where they can view al...

8. [New Acellus Feature: Recovery Mode Enhancements](https://www.science.edu/acellus/2021/02/new-acellus-feature-recovery-mode/) - The “Recovery” features in Acellus have been updated to provide further support to students who are ...

9. [Accredited Online Elementary School | Acellus Academy](https://www.acellusacademy.com/online-elementary-school/) - Explore Acellus Academy's online elementary school program with engaging courses and activities in m...

10. [[PDF] Vectored Instruction - Acellus](https://www.acellus.com/schools/wp-content/uploads/2024/11/Vectored-Instruction_v241101.pdf) - This guide will cover where to find the data for Vectored Instruction, how to utilize the data shown...

11. [Accredited Online Middle School - Acellus Academy](https://www.acellusacademy.com/online-middle-school/) - Acellus Academy's online middle school program is designed to build on the foundational skills stude...

12. [Monitoring Goals/Progress | Power Homeschool](https://www.powerhomeschool.org/support/monitoring-goals-progress/) - Monitoring goals in Acellus is simple and flexible. Learn how to track weekly progress for your stud...

13. [Acellus Gold Edition](https://www.acellusacademy.com/acellus-gold-edition/) - Acellus Gold Edition offers lessons filmed in high-definition and interactive learning activities to...

14. [Tutorial – Setting Goals | Acellus Academy](https://www.acellusacademy.com/tutorials/setting-goals/) - Welcome to this step-by-step guide designed to help Acellus Academy parents change the weekly goal f...

15. [[PDF] Student Goals_v241101 - Acellus](https://www.acellus.com/schools/wp-content/uploads/2024/11/Student-Goals_v241101.pdf) - In the 'Courses' section, teachers can set a weekly goal for each course. The default weekly goal is...

16. [[PDF] Course Modes_v241115 - Acellus](https://www.acellus.com/schools/wp-content/uploads/2024/11/Course-Modes_v241115.pdf) - This tutorial will guide you through the process of adjusting the course modes for students in Acell...

17. [Mastering Acellus: A Student's Guide - ROCS Helpdesk](https://help.rocs.org/support/solutions/articles/154000184766-mastering-acellus-a-student-s-guide) - This guide provides detailed instructions on how to navigate and use Acellus, the primary platform u...

18. [[PDF] Report Card - Acellus](https://www.acellus.com/schools/wp-content/uploads/2024/11/Report-Card_v241112.pdf) - Each Course Tile displays the projected completion date, daily goal progress, course progress and th...

19. [Retaking Steps/Rewatching Videos - Power Homeschool](https://www.powerhomeschool.org/support/retaking-steps-rewatching-videos/) - Students are able to go back and retake steps that they have already completed. This allows them to ...

20. [Vectored Instruction : r/Acellus_Academy - Reddit](https://www.reddit.com/r/Acellus_Academy/comments/1mf98qu/vectored_instruction/) - Vectored instruction will go towards the student's daily goals, and it will be tracked towards the n...

21. [How to Check Progress in Acellus - YouTube](https://www.youtube.com/watch?v=IRVxWKJoTF0) - Explore simpler, safer experiences for kids and families. Learn more. Comments are turned off. Learn...

22. [Tutorial – Monitoring Student Progress | Acellus Academy](https://www.acellusacademy.com/tutorials/student-progress/) - Courses: Here you can quickly see your student's progress in each active course, along with their gr...

23. [Explore the Features of Acellus Gold Edition - YouTube](https://www.youtube.com/watch?v=endVssqni-s) - ... activities that help motivate students, and a dynamic learning experience for grades K-12. Learn...

24. [Acellus Releases New Video Features - Save Place, Skip Back](https://www.acellus.com/2018/12/save-place-rewind-feature/) - The other feature — “Skip Back” — allows them to skip back in the video they are watching to reinfor...

25. [Tech Support Help | Power Homeschool](https://www.powerhomeschool.org/support/tech-support-help/) - Acellus has taken a new approach on helping students 'recover' their Exam scores (building upon prev...

26. [Exams – Retry for Extra Credit - Acellus](https://www.acellus.com/2017/02/exams-retry-for-extra-credit/) - We recently announced the Exam Recovery Mode, a way in Acellus for students who fail an exam to auto...

27. [Making work pages on Website accessible for screen readers by Eme](https://www.youtube.com/watch?v=odJPhATZTuY) - Acellus- Making work pages on Website accessible for screen readers by Eme. 954 views · 5 years ago....

28. [Using the Speech Controller with Acellus - YouTube](https://www.youtube.com/watch?v=EGn4M3XQ4WM) - Share your videos with friends, family, and the world.

29. [Acellus Academy: How to retake final exams for best grades.](https://www.youtube.com/watch?v=4LZ9GACrTM0) - Are you struggling to pass your final exams? Do you feel like you're never going to achieve your tar...

30. [Technical Support for Schools - Acellus](https://www.acellus.com/schools/support/technical-support/) - The Acellus App is available for free on all major platforms, including Windows, macOS, Android, iOS...



---

## Perplexity Deep Research Update (2026-02-16 15:54)

- On the Acellus Course Page inside the macOS native app, each Course Tile (e.g., Social Studies) is *not* exposed as an `AXButton` or `AXLink`; instead, the tile’s text and goal indicators appear as separate nodes: `AXStaticText "<Course Name>"`, `AXGroup "3 Steps"`, `AXStaticText "START"`, and nearby `AXImage`, all under the `AXHeading "Classes"` group. The actual clickable region is a non‑AX area rendered inside JavaFX WebView. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/78703942/1fedb283-a498-427b-8ff1-f03c56158c8e/escalation_context_consult_1771256736_d91f917c.md)
- Automation must use coordinate‑based `mouse_click` anchored to these static text nodes (for example, clicking slightly below/right of `AXStaticText "Social Studies"` or at the center of `AXStaticText "START"`) to activate a course tile. `ax_press` on these nodes will fail because they are not actionable AX roles. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/78703942/1fedb283-a498-427b-8ff1-f03c56158c8e/escalation_context_consult_1771256736_d91f917c.md)
- The Notifications and Account sections (headings “Notifications”, “Account”, buttons “Profile”, “Sign Out”) are part of the main layout, not a blocking modal overlay. Course‑tile nodes (course name, steps, START) are present in the same `AXWebArea "Acellus Student"`, so there is no need to dismiss Notifications/Account before clicking a course tile. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/78703942/1fedb283-a498-427b-8ff1-f03c56158c8e/escalation_context_consult_1771256736_d91f917c.md)
- JavaFX WebView in the Acellus macOS app exposes only partial HTML content to macOS accessibility APIs: frame controls and some static text/images are visible, but clickable composite regions like course tiles may not appear as distinct interactive AX elements. This requires Taey‑Ed to combine AX anchors with coordinate clicks and to validate transitions using both tree changes and screenshots. [download.jonathangiles](https://download.jonathangiles.net/downloads/presentations/2015/Accessibility.pdf)
- There is no evidence that the Acellus Course Page supports reliable Tab/Enter keyboard navigation between course tiles; student guides and screen‑reader workflows consistently describe *clicking* a course on the dashboard to start or continue, and your captured tree shows no focusable `AXButton`/`AXLink` wrapping the tiles. Keyboard‑only strategies should not be used for tile activation on this screen. [help.rocs](https://help.rocs.org/support/solutions/articles/154000184766-mastering-acellus-a-student-s-guide)


---

## Perplexity Deep Research Update (2026-02-16 18:07)

```markdown
### ACELLUS_ASSIGNMENT_COMPLETE Screen

**Screen classification**: TRANSITION (simple advance)

**Layout**: Full-screen overlay on top of the next quiz/lesson content. Shows:
- "ASSIGNMENT COMPLETE" heading
- Lesson name (e.g., "Calculating Credit Card Interest")  
- Star rating, YOUR SCORE (e.g., 100%), ACCURACY, TIME SPENT
- Gold earned display
- "Post to your feed" AXButton (social sharing, SKIP)
- "Move On" AXButton with ">" chevron (PRIMARY ACTION)
- Next lesson content (quiz question + answer buttons) already loaded underneath

**Critical AX quirk**: The "Move On" AXButton has an inflated bounding box 
([930, 46] size [30, 376]) covering the entire score card area. The actual 
clickable ">" chevron is a ~30x30px region at the BOTTOM-RIGHT of the AX bounds.
Do NOT click at center — use y_offset_pct=0.96 or focusenter strategy.

**AXPress does not work**: JavaFX WebView does not reliably propagate AXPress 
actions on this button. Use mouse_click with bottom-right offset or focusenter.

**Recommended strategy priority**:
1. focusenter (bypasses coordinate issues entirely)
2. mouse_click with y_offset_pct=0.96 (targets actual chevron)
3. focusspace (alternative keyboard activation)

**Timing**: postdelay 4.0s minimum — the overlay dismissal + underlying page 
activation is slow in JavaFX WebView.

**Resources panel**: May or may not be open. Close it first (optional: true) 
to remove any z-index/event-capture interference. Resources panel elements are 
at x=1462+ and include Back, Close, How to Use Acellus, Textbook, Help Videos, 
Lesson List, Ask a Question, Submit Feedback, Course Overview, Policy Link.

**Expected next screens**: ACELLUS_VIDEO_PLAYING, ACELLUS_QUIZ_CHOICE, 
ACELLUS_DAILY_GOAL_MET, ACELLUS_COURSE_PAGE

**"Post to your feed" button**: IGNORE — social sharing CTA, not educational. 
Its AXButton bounds ([707, 470] size [341, 355]) partially overlap "Move On" 
in x-range and may intercept clicks if targeting the wrong coordinates.
```
