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
