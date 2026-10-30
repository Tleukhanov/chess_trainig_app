# Changelog

## 0.9.10 — 2026-10-30

Icon-first error feedback with a short, engine-grounded “why”.

- Game review error rows and opening-report moments now show the badge icon and
  the move class first; win%, loss and engine lines live under
  “Детали движка”.
- Added `facttext.why_text()`: capture/check/mate/promotion are named from
  FEN and SAN; quiet best moves use a factual “you played X, engine preferred Y”
  contrast. No invented threats or motifs.
- `/api/games/{id}/review` items now include `why`, `fen_before` and
  `best_line`; opening moments include `why`, `fen` and `best_line`.
- The game error block now also includes inaccuracies, not only blunders and
  mistakes; in-book and `missed_win` errors are kept via analysis classification.
- `why` is suppressed when the engine best move equals the played move, so
  “Ты не забрал …” cannot contradict the move actually played.
- Clicking an opening moment opens the position *before* the move; expanding
  “Детали движка” no longer opens the explorer. Move labels use chess notation
  (`10. Qd2`, `13...bxa3`) instead of “полуход N”.
- Review-line details no longer duplicate the best move or the cause label.
- Opening reports were regenerated with one deterministic Stockfish pass at
  depth 18, single-threaded, so best move, line, loss and classification come
  from the same search.
- Fixed the opening recommendation wording: “Вместо <move>: …” instead of
  “Повтори линию после <move>”.

## 0.9.7 — 2026-10-29

- Review graph/coach bubble use post-move `win_after`.
- Task solution/explanation hidden until answer or reveal; a wrong answer no
  longer marks the daily task as solved.
- Removed the unconditional “missed tactics” cause and added the worst moment
  to the game summary.
- Unified badge labels/plurals and added floors for `great`/`brilliant`.
- Opening reports no longer mix depth-14 loss with depth-18 best moves.
- Hid internal LLM tools and the all-users table from the user-facing demo.
- Suppressed false weak-opening and trend claims on tiny samples.

## 0.9.6 — 2026-10-28

- Mobile navigation: hamburger menu below 720px.
- Mobile form controls at 16px to avoid iOS focus zoom.
- Larger day-plan checkboxes and move cells.
