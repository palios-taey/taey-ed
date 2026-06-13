# UNKNOWN Classification Guide — khan_academy
Used only when exact hash lookup and signature matching both miss. The
classifier must output one canonical on-disk screen_type filename or UNKNOWN.
Never emit a bare master like NAVIGATION, ARTICLE, VIDEO, or TRANSITION.

## Allowed screen types
- `NAVIGATION__COURSE_DASHBOARD`: course-overview / mastery dashboard with
  "Course mastery", mastery challenge cards, unit crown icons, many curriculum
  links, and no answer widgets or player.
- `NAVIGATION__UNIT_OVERVIEW`: one specific unit page with "UNIT N <title>",
  lesson/skill rows inside that unit, and no article/player/answer widgets in
  the main content.
- `NAVIGATION__LESSON_LIST`: one specific lesson page with "Lesson N:
  <title>" and a chronological list of video/article/exercise pieces.
- `ARTICLE__READING`: article-renderer text content to read, no graded widgets,
  and an "Up next" affordance, but not already marked completed.
- `ARTICLE__COMPLETE`: completed article/content state with completion markers
  like "completed Article <X>" or "completed Video <X>" plus an "Up next" link.
- `VIDEO__PLAYER`: any embedded video player state. "Play video" and "Up next"
  can both be present before, during, or after playback; the player itself is
  the signal.
- `TRANSITION__INTRO`: start/interstitial states like "Let's go", "Start quiz",
  "Start Unit test", or a single-button "Keep going" modal.
- `TRANSITION__SUMMARY`: post-set summary / review / completion states with
  mastery points, "(Choice X, Correct)" markers, "Show summary", or "Up next".
- `EXERCISE_MULTIPLE_CHOICE`: single-answer choice question rendered with
  `(Choice X)` prefixed rows.
- `EXERCISE_MULTIPLE_SELECT`: select-all / choose-N question with multiple
  `(Choice X)` rows.
- `EXERCISE_DROPDOWN`: one or more answer comboboxes like "Select an answer".
- `EXERCISE_TEXT_INPUT`: answer text field/area in the question region.
- `EXERCISE_TABLE_INPUT`: repeated per-cell answer fields or per-row selectors.
- `EXERCISE_GRAPH_POINTS`: graph/plot point handles such as "Point N at X, Y".
- `EXERCISE_LABEL_IMAGE`: image labeling task with clickable dots/selectors.
- `EXERCISE_MATCHER`: draggable bank -> target pairing matcher.
- `EXERCISE_SORTER`: ordering / ranking / bucket sorting interaction.

## Decision order
1. If the main content is a video player, return `VIDEO__PLAYER`.
2. If the main content is article text, distinguish `ARTICLE__READING` vs
   `ARTICLE__COMPLETE` by completion markers.
3. If the main content is links/cards with no player/widgets, distinguish the
   three `NAVIGATION__*` subtypes by course vs unit vs lesson scope.
4. If the screen is just a forward-only interstitial or completion/review card,
   return `TRANSITION__INTRO` or `TRANSITION__SUMMARY`.
5. If answer widgets are present, choose the matching `EXERCISE_*` subtype.
6. If none of the allowed types fit cleanly, return `UNKNOWN`.
