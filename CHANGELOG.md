# Changelog

## 0.9.14 — 2026-11-03

- Game IDs cited in the coach commentary are now clickable links. They open
  the game inside the app at the exact ply when the evidence provides one;
  no external Lichess jump is required.
- 894 tests pass.

## 0.9.13 — 2026-11-03

- Weekly plan now shows evidence, not just an average: “N% партий — ошибки в
  фазе X (a из b)” and the worst concrete example with game, move, loss and a
  button that opens the game at that ply.
- The LLM commentary input receives the same evidence and is instructed to
  mention the percentage and the concrete example; it is still a commentary,
  not a second plan.
- Fixed the Russian phase task wording (“в миттельшпиле”, not “в миттельшпиль”).
- The LLM block is now an explanation prompt, not another plan; it must cite
  the percentage and the concrete example. Old cached plan texts are hidden
  after the prompt-version change and can be regenerated with one click.
- 894 tests pass.

## 0.9.12 — 2026-11-03

- Plan page no longer looks like two competing weekly plans. The deterministic
  “План на неделю” comes first; the LLM text is now “Комментарий тренера”
  inside a collapsed “Почему такой план” explanation. The generation button
  is “Собрать комментарий”, not “создать ещё один план”.
- Remove duplicate “Дрели в кеше” stat card.
- 892 tests pass.

## 0.9.11 — 2026-11-02

Security hardening before a public deployment.

- HTTP requests now use only the session cookie; removing the `meta`
  `current_user` fallback closes the “act as last logged-in user” hole.
- `/api/users` and legacy `add`/`switch` are admin-only; `delete`/`fide` are
  self-or-admin; foreign job and foreign game access return 404.
- `ADMIN_NICKS` no longer grants rights by itself: it only promotes existing
  profiles at startup. Admin nicks are reserved against self-registration.
- Passwordless legacy profiles are no longer claimable by default; local
  migration requires `ALLOW_LEGACY_CLAIM=1`.
- Registration validates nicks, the FIDE cache path is traversal-safe, and
  cohort CSV escapes formula prefixes.
- Deleting a profile removes `data/<nick>/`, per-user daily plan/log/rating
  snapshots, user meta, persisted job snapshots and in-memory jobs, so a later
  owner of the same nick cannot inherit old data.
- First admin bootstrap: `ADMIN_BOOTSTRAP_PASSWORD` creates a missing
  `ADMIN_NICKS` profile once; existing passwordless admins get the password
  without overwriting an existing one.
- FIDE jobs have a separate light per-user cap; humanize still goes through the
  heavy engine limit. Session cookies are `Secure` on HTTPS, including
  `X-Forwarded-Proto` behind a proxy.
- A PGN import cannot reuse a game id already cached by another player;
  analysis is hidden if a foreign copy exists.
- README refreshed for the current web + CLI product, security/bootstrap
  settings and deployment requirements; `python -m trainer --version` now
  reports 0.9.11.
- 892 tests pass.

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
