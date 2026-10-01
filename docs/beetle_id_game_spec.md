# Beetle ID Game: Specification

Status: built on branch `claude/funny-tesla-pi0iki` (PR #255). Classifier integration is out of scope (see section 12).

## 1. Purpose

1. Collect species labels for images that don't have verified labels yet.
2. Measure how reliable each player's labels are, using validated images the player doesn't know are being scored.
3. Let proven experts' labels reach curators as trusted proposals.
4. Help players learn through feedback after each round, and let them flag labels they think are wrong.

## 2. Access and placement

| Requirement | Detail |
|---|---|
| Entry point | "Beetle ID Game" card on the home page, plus an entry in the mobile and desktop sidebars |
| Players | Logged-in users only. Accounts are created by staff |
| Staff | Review page, player reports, proposals and report handling on the annotation page |
| Devices | Mobile-first: native pickers, large tap targets, action bar within thumb reach, tap a photo to see the whole image |
| Style | Matches the site: gray palette, `font-semibold` headings, `rounded-lg` gray-200 panels, Flaticon icons |

## 3. What's in the game

- **Playable ROI:** a `Beetles` row with a bounding box (width and height > 0), not soft-deleted, on a live image that has a file.
- **Scored items ("checks"):** playable ROIs with `bbox_is_validated = True`, a taxon with at least subfamily and genus, and no open player report.
- **Unscored items:** playable ROIs with `bbox_is_validated = False`. These are where labels are collected.
- **Taxonomy:** four ranks: subfamily, tribe, genus, species. Subtribe isn't used.
  - Malformed rows in the species list (shifted columns, e.g. an epithet in the subfamily column) are excluded.
  - Subspecies rows are merged into their genus + species.

## 4. Game modes

### 4.1 Name the beetle (classify)
- Shows a crop of one ROI (the box plus 8% margin). Tap to see the whole image with the box outlined.
- The player answers with cascading pickers (subfamily → tribe → genus → species), where each list narrows the next.
  - Picking a genus fills in its subfamily and tribe.
  - A search box jumps straight to a genus or species.
- Players can stop at any rank. Unanswered ranks stay "Not sure" and aren't scored.
- Answers must match the taxonomy, otherwise they're rejected.

### 4.2 Spot the relatives (pair)
- Shows two ROI crops side by side, labelled A and B.
- One choice: different subfamily / same subfamily / same tribe / same genus / same species / not sure.
- **Scored pairs:** two validated ROIs.
- **Unscored pairs:** one unvalidated ROI next to one validated ROI, so the answer implies a label for the unvalidated one.
- The partner is picked at a random relationship (same species, genus, tribe, subfamily, or different), so answers are spread across ranks.
- The two ROIs always come from different images, and their left/right order is random.

### 4.3 Both modes
- Every item has a **Skip** button.
- There's no feedback during a round: nothing says right or wrong, and the score doesn't change.
- The browser receives only an image URL and a box, never the ROI ID, its label, or whether the item is scored.

## 5. Rounds

| Rule | Value |
|---|---|
| Items per round | 10 (`GAME_ROUND_SIZE`) |
| Share of scored items | 60% until the player has 20 scored answers in that mode, then 20% (`GAME_CHECK_RATIO_NEW` / `_KNOWN`, `GAME_CALIBRATION_CHECKS`). At least one per round |
| Pool shortfall | If one pool runs out, the round is topped up from the other |
| Item order | Shuffled |
| Resume | Reloading picks up an unfinished round from the last 12 hours (`GAME_RESUME_HOURS`) |
| Loading speed | The next item's photos are downloaded while the current one is answered |
| Safety | Answers must arrive in order. A duplicate or out-of-order submit gets a 409 and the round resumes |
| Timing | The response time for each answer is recorded |
| Unscored repeats | Items the player already answered are avoided, and only reused if the pool is too small |
| Scored repeats | **Never.** A validated ROI the player has been shown (as a scored item or a pair partner) is never scored for them again |

## 6. Choosing images: difficulty and focus

- **Difficulty (0 = easy, 1 = hard)** is stored per ROI in `RoiDifficulty`:
  - `model_difficulty`: an empty slot for classifier output, which takes priority once filled.
  - `game_difficulty`: learned from answers. For scored items it's the error rate (smoothed); for unscored items it's how much players disagree on the genus. It's recomputed at the end of each round.
  - Unknown difficulty counts as 0.5.
- **Each player's target difficulty** is `min(0.9, 0.2 + 0.02 × rounds finished + 0.3 × skill)`, where skill runs from 0 (coin-flip accuracy) to 1 (perfect). So the game always gets harder over time, and faster for accurate players.
- **Selection:** about 6 random candidates are drawn per slot (by jumping to a random ID rather than sorting the table randomly), then weighted toward ones near the target difficulty.
- **Focus:** half of the scored items in a classify round come from genera or tribes the player has recently been labelling but hasn't proven themselves in yet, so they get the chance to earn trust there.

## 7. Scoring

- **Classify:** each answered rank is marked right or wrong against the validated taxon. Species counts as right only if genus and epithet both match. Ranks the player left blank, or that the reference lacks, aren't judged.
- **Pair:** the answer "same X" claims the pair shares every rank down to X and differs below it. Each of those claims is judged. "Not sure" isn't judged.
- **Accuracy** is correct judged ranks ÷ judged ranks, counting scored answers only and excluding held answers (section 9). It's shown once the player has at least 10 judged ranks.
- **Reliability weight** for combining votes is `(correct + 1) / (judged + 2)` per rank.

## 8. Expertise and trusted labels

- **Skills are tracked per rank within a branch:**
  - species within a genus
  - genus within a tribe
  - tribe within a subfamily
  - subfamily overall
- Only scored "Name the beetle" answers count. Each validated ROI counts once, using the player's first answer.
- **Proven** means at least 15 distinct scored examples in that branch, with the 95% lower bound on accuracy (Wilson score) at least 0.90. With the defaults, a perfect record needs **35** answers. Settings: `GAME_TRUST_MIN_JUDGED`, `_MIN_LOWER_BOUND`, `_Z`.
- **Untestable branches:** a branch with fewer validated ROIs than the number needed to prove competence accepts proof in 2 or more sibling branches instead (`GAME_TRUST_SIBLINGS`).
  - Species in an untested genus → species-level proof in 2 other genera of the same tribe.
  - Genus in an untested tribe → proof in 2 other tribes of the same subfamily.
  - Tribe in an untested subfamily → proof in other subfamilies.
- **Expert-backed label:** at a given rank, at least 1 proven player supports the winning value (`GAME_TRUST_MIN_VOTES`), no proven player disagrees, and every rank above it is expert-backed too.
- **Proposals are for staff to review:** nothing writes to ROI records automatically.
- Skills are recomputed at the end of each round and whenever a report is resolved.

## 9. Feedback and reports

### 9.1 After a round
- A "Your answers" page for every finished round, reached from the round summary and from "My performance".
- **Verified items:** the player's answer next to the validated label, ✓/✗ per rank, and an overall verdict (Right / Partly right / Not quite).
- **Unverified items:** "Not verified yet", the current (unverified) label, and what other players have said.
- **Pairs:** the true relationship when both are verified, and each image's label.
- A summary line: "X of Y verified beetles fully right".
- **Changed requirement:** players are unaware which items are scored *during* a round. After a round, feedback reveals it. Section 5's never-repeat rule protects scoring integrity.

### 9.2 Reporting an ROI
- From the review page, a player can report any image: the name looks wrong, the box doesn't fit, a photo problem, or something else, plus an optional note. One open report per player per ROI.
- While a report is open:
  - The ROI is excluded from scoring for everyone.
  - The reporter's scored answers on it are held (`GameAnswer.score_hold`), so reporting never costs them.
- **Resolved by staff:**
  - **"Label was wrong · fixed":** every scored answer on the ROI (in either mode) is re-scored against the corrected label. If the ROI is no longer validated, those answers are voided for everyone.
  - **"Label is correct":** the held answers count again.
- A report does **not** change `bbox_is_validated` by itself.

## 10. Label proposals for curators

- **Consensus per unvalidated ROI:** votes from both modes, weighted by each player's reliability at that rank.
  - A pair answer "same genus" votes for the validated partner's subfamily, tribe and genus.
  - "Different subfamily" and "not sure" imply nothing.
- **Annotation page (on each ROI):**
  - The game proposal (value, weighted support and votes per rank, with a shield on expert-backed ranks).
  - **Use \<species\>:** sets `depicts_valid_name_id`. It doesn't validate the ROI, and it's refused if another user has the image locked.
  - **Dismiss.**
  - Both decisions are logged in `LabelReview`.
- Open player reports are shown on the ROI with the two resolve buttons. A link of the form `?image=<id>` opens a specific image.

## 11. Pages and reports

| Page | Who | Content |
|---|---|---|
| `/game/` | Players | Items labelled, accuracy, the two modes, leaderboard (sort by labelled or accuracy) |
| `/game/play/<classify\|pair>/` | Players | The game |
| `/game/rounds/<id>/` | The round's owner, or staff | Feedback on that round and reporting |
| `/game/me/` | Players | Headline stats, challenge level, expert areas, progress toward the next ones, accuracy by rank and by month, recent rounds |
| `/game/players/<id>/` | Staff | Any player's report |
| `/game/review/` | Staff | Open reports queue, player table with expert badges, proposals (all or expert-backed), CSV exports: proposals, reliability, expertise |
| Django admin | Staff | GameRound, GameAnswer, PlayerSkill, LabelReview, RoiDifficulty, GameReport |

Privacy rules for the player report:

- Progress is listed only for groups the player has named themselves, and only after at least 5 scored answers there (`GAME_REPORT_MIN_JUDGED`), so the report never gives away the true label of a scored item.
- Scores and reports only update at the end of a round.

## 12. Data model (migrations 0018–0020)

| Table | Purpose |
|---|---|
| `game_round` | A player's round: mode, the item list chosen when it started, start and finish times |
| `game_answer` | One answer: the ROI(s), the per-rank answer or pair answer, per-rank correctness, a snapshot of the reference label, response time, `score_hold` |
| `game_player_skill` | Correct / judged / lower bound / proven / proven-at, per player, rank and branch |
| `game_label_review` | Staff accept or dismiss decisions on proposals |
| `game_roi_difficulty` | Difficulty per ROI: classifier slot and game-derived value |
| `game_report` | Player reports and how they were resolved |

## 13. Out of scope / future

- Superusers uploading classifier predictions, and running the classifier on new images or bulk uploads. These will fill `RoiDifficulty.model_difficulty` (and later could feed model proposals).
- Optional: unvalidate an ROI automatically after N reports from different players.
- Optional: a notice to reporters when their report is resolved.

## 14. Settings summary

| Setting | Default |
|---|---|
| `GAME_ROUND_SIZE` | 10 |
| `GAME_CALIBRATION_CHECKS` | 20 |
| `GAME_CHECK_RATIO_NEW` / `_KNOWN` | 0.6 / 0.2 |
| `GAME_MIN_JUDGED_FOR_ACCURACY` | 10 |
| `GAME_REPORT_MIN_JUDGED` | 5 |
| `GAME_RESUME_HOURS` | 12 |
| `GAME_DIFFICULTY_START` / `_PER_ROUND` / `_SKILL_WEIGHT` / `_MAX` | 0.2 / 0.02 / 0.3 / 0.9 |
| `GAME_CANDIDATE_OVERSAMPLE` | 6 |
| `GAME_TRUST_MIN_JUDGED` | 15 |
| `GAME_TRUST_MIN_LOWER_BOUND` | 0.9 |
| `GAME_TRUST_Z` | 1.96 |
| `GAME_TRUST_SIBLINGS` | 2 |
| `GAME_TRUST_MIN_VOTES` | 1 |

## Update: one continuous feed (supersedes sections 4.3, 5 and the round wording elsewhere)

- **No rounds for the player.** They get a continuous feed of beetles until they tap **Exit**. Behind the scenes the feed is
  still stored as batches (`GameRound`, 10 items), so scoring, skills, difficulty and the "Your answers" pages are unchanged;
  when a batch ends the next one starts in the same response and the player never sees a break. The feed only ends
  ("You're all caught up") when there is nothing new left to show (`start_round(..., fresh_only=True)`).
- **Checks are dropped in now and then.** Within a batch the scored items are spread evenly with a random start (`game.spread`),
  instead of a plain shuffle, so they are neither clumped nor a predictable rhythm. The 60% / 20% ratio is unchanged.
- **Names:** *Name the beetle* is **Name That Beetle**; *Spot the relatives* is **Family Ties**. (Display only: the stored mode
  values and model labels are unchanged, so there is no migration.)
- **No live score.** There is no progress bar, count or accuracy while playing. Scores are on the game home and in
  My performance. Leaving with **Exit** closes the current batch (`game_exit`) so the answers count at once; a batch that was
  left open (tab closed) is closed the next time they open the game home (`close_idle_rounds`, after 10 idle minutes).
- **Confetti** for a scored item answered right: the species in Name That Beetle, or every judged claim in Family Ties
  (never for "Not sure"). It is the only hint that an item was scored, and only when the player won.
- **Layout (phones first).** One fixed screen: header (Exit, title, search/help), photos, answer panel, and a fixed action row.
  Nothing moves between beetles, so the buttons are always in the same place.
  - Name That Beetle: four stacked lists, broad to specific (subfamily, tribe, genus, species), then **Skip** / **Next**.
    Search by name is a button in the header.
  - Family Ties: a vertical ladder from *different subfamily* (top) to *same species* (bottom) that fills like a meter and can be
    tapped or dragged, then **Not sure** / **Next**. On a phone the two photos sit one above the other.
  - Colours: grey, black and white only. Colour appears only for results and errors, and on the confetti.

## Update: keeping people playing (rewards)

All derived from the player's answers (`game_rewards.py`), nothing new is stored, no migration.

- **Daily goal** (`GAME_DAILY_GOAL`, default 20 beetles) and a **day streak** (consecutive days with an answer; still alive
  until the day after the last one ends). The feed's header shows a small `today/goal` chip; its flame turns amber only when
  the goal is met, because that is what the colour means.
- **Levels** from the number of beetles labelled: Egg, Larva, Pupa, Young adult, Beetle scout, Field entomologist,
  Taxonomist, Beetle master, Coleopterist.
- **Badges**: first steps, 10 / 100 / 1,000 beetles, 3 / 7 / 30-day streaks, daily goal, both games, first species right,
  25 species right, trusted expert.
- **Toasts while playing** for a level up, the daily goal, the first answer of a streak day and count milestones (10, 25, 50,
  100, 250, 500, 1,000). They never mention accuracy (a test checks the words), so there is still no live score.
- **Recap when you tap Exit**: beetles labelled this sitting, how many of the known beetles were right (the one place the
  score appears, after leaving), the streak, any new badge. Then "Keep playing" or the game home.
- **Game home**: level and progress bar, daily goal, streak, all badges (earned and locked), and a leaderboard that can be
  *this week* (resets Monday) or all time, by beetles labelled or accuracy.

## Update: points (game_scoring.py)

Every answer is worth points (`AnswerPoints`); a player's score is the running total, never below zero (`PlayerScore`).
Full rules for players are on the "How scoring works" page (`/game/how-it-works/`).

- **Validated beetles (truth)** earn the most. Name That Beetle: subfamily 1, tribe 2, genus 4, species 8 when right,
  minus 75% of that when wrong (`GAME_POINTS_WRONG_FACTOR`). Family Ties: by the true relation, different subfamilies 1,
  subfamily 2, tribe 3, genus 5, species 5, plus up to 25% for alike photos (photographer, institution, magnification,
  country, aspect; `GAME_POINTS_SIMILARITY_BONUS`); wrong loses 1 per step off (`GAME_POINTS_PAIR_STEP`).
- **Unvalidated beetles (agreement)**: up to 60% (`GAME_POINTS_CONSENSUS_CAP`) of the truth points, never negative.
  Judges are players with at least `GAME_RATER_MIN_JUDGED` (10) judged ranks whose *rating* is at or above the median.
  A proven expert for that branch weighs 1; anyone else weighs `1 / (1 + exp(-(their rating - your rating) / 0.1))`.
  Per rank, agreement `c = (agree - disagree) / (agree + disagree + 1)`, points `cap x rank points x max(0, c)`.
- **Rating**: the lower end of a Wilson interval (z = 1) of the player's judged ranks on validated beetles,
  first sightings only. It is what decides whose agreement counts; the score is what decides unlocks.
- **Not sure / skip**: -0.25 (`GAME_POINTS_UNSURE`).
- **Retries**: a validated beetle answered wrong comes back after `GAME_RETRY_AFTER_DAYS` (2), at most
  `GAME_RETRY_MAX` (3) times, `GAME_RETRY_PER_BATCH` (1) per batch, marked "Seen before". It earns half
  (`GAME_POINTS_RETRY_FACTOR`) and is left out of accuracy and expertise (`GameAnswer.is_retry`).
- **Retroactive**: points are recomputed for a player when they leave the game and for everyone nightly
  (`manage.py recompute_game_scores`, "Nightly game scores" workflow). Answers on beetles validated since are then
  scored against the truth.

## Update: levels, perks, focus, expertise tree (game_levels.py)

- **Levels need points and reliability** (reliability = the rating). Egg (0), Larva (50: focus a subfamily),
  Pupa (150, 35%: focus a tribe), Young adult (400, 50%: focus a genus), Beetle scout (800, 60%: **labels go to
  curators as suggestions**), Field entomologist (1500, 70%), Taxonomist (3000, 75%), Beetle master (6000, 80%),
  Coleopterist (10000, 85%). Levels can drop if reliability drops; perks follow the current level.
- **Suggestions to curators** (annotation page proposals) only count answers from players at the suggestions level
  or proven experts somewhere (`GAME_PROPOSALS_NEED_LEVEL`, default on).
- **Experts' labels without review**: when at least `GAME_AUTO_APPLY_MIN_EXPERTS` (2) proven experts agree down to
  species with no expert disagreeing, on a beetle with no species label, not validated and never reviewed, the species
  is written to the beetle (still unvalidated) with a `LabelReview` that has no reviewer
  (`GAME_AUTO_APPLY_EXPERT_LABELS`, default on). Runs when a player leaves the game (their round's beetles) and nightly.
- **Experts must also be in the top `GAME_EXPERT_PERCENTILE` (25%) by rating** once `GAME_EXPERT_MIN_PLAYERS` (10)
  players are rated.
- **Focus** (`GamePreference`): a player can limit the feed to one subfamily / tribe / genus when unlocked; falls back
  to everything when the focus has no beetles left.
- **Expertise tree** (`/game/expertise/`, anyone's at `/game/players/<id>/expertise/`): subfamily > tribe > genus,
  each coloured by accuracy (grey too few, red <60%, amber 60-85%, green 85%+, glowing green proven expert).
- **Unlocks page** (`/game/unlocks/`): the ladder, what is missing for the next level, what happens to your labels,
  and the focus picker.

## Update: the quick loop

- **After each Name That Beetle answer** a small card says what other players said about that beetle (their latest
  answer each, never the truth): the most common name at the most specific rank most of them reached, how many,
  and whether you agree (green only when you do). The first to name a beetle is told so.
- **Before answering** a beetle others have named shows "Named by N other players" (the count only, so nobody is led).
- **Beetles others named come first**: about half (`GAME_PEER_SHARE`) of the unvalidated beetles in a batch are ones
  1 to `GAME_PEER_MAX_OTHERS` (4) other players have named and you have not, so names get second and third opinions.
- **Combo**: answers in a row without skipping show as x3, x4... in the header, with a toast at 10, 25, 50, 100.
- **Participation**: every real answer earns `GAME_POINTS_PARTICIPATION` (0.5) on top of its accuracy points, so the
  score grows with play. Skips do not.

## Update: leaderboard, profiles, annotation tips, help

**Leaderboard** (`/game/leaderboard/`, `game_board.py`). Ranks players by score, accuracy (shown after
`GAME_MIN_JUDGED_FOR_ACCURACY` judged answers) or beetles seen, all time or this week, with a name search. The
**specialists** board ranks players inside one subfamily (by their tribe answers), tribe (genus answers) or genus
(species answers), proven experts first, then by the cautious (Wilson) estimate. The game home shows the top 10.

**Profiles** (`/game/players/<id>/profile/`). Anyone signed in can open a player from the leaderboard: level, score,
accuracy, beetles seen, streak, games played, where they are a proven expert, badges, and a link to their tree.

**Tips on the Image Annotation page** (`game_tips.py`, served with `/game/api/proposals/` as `tips`). Only answers
from players whose labels reach curators count (`game_levels.suggestion_voters`).

* *Agreement*: the deepest rank backed by a proven expert, or by at least `GAME_TIP_MIN_VOTES` (3) players with at
  least `GAME_TIP_MIN_SUPPORT` (75%) of the weighted vote. Marked amber when it differs from the current label.
* *Not in*: a Family Ties answer against a validated beetle also says what the other beetle is not ("same tribe"
  means not the partner's genus; "different subfamily" means not its subfamily). Shown when at least
  `GAME_TIP_MIN_NOT_VOTES` (2) players say so and at least 75% of those who spoke to it agree.

**Help and feedback.** `/game/how-it-works/` opens with a 30-second guide, then scoring, levels, where labels go,
experts and the leaderboard. A link to the game's GitHub Discussions category (`GAME_DISCUSSIONS_URL`) is on the
game home, the help page and the end-of-game recap.
