# Changelog

## 0.9.22 — 2026-11-12

Release hygiene (audit M9). No feature work, no algorithm changes, no data migration.

- Docs: honest test counts — with `LLM_API_KEY` set, 1069 tests pass (4 skipped,
  including 20 new release-hygiene tests); without it, 6 `tests/test_middlegame_api.py`
  tests fail (1063 pass). Previous unqualified "all tests pass" wording removed;
  verified compatibility is claimed only for the keyed run.
- Deployment docs: target baseline is a VPS (2 vCPU / 4 GB RAM) with a persistent
  disk for `data/`. `Procfile`/`Aptfile`/`app.json` are marked as legacy Heroku
  leftovers — not the current deployment, not recommended; they were kept, not
  deleted, and no secrets were added.
- README states the project is private, has no public self-serve support, and no
  LICENSE (owner decision: closed/commercial direction, not open source).
- `opencode.json` (local dev-only MCP config, no secrets) removed from the repo.
- `PLAN_M9.md`: leaked machine-specific absolute path removed (now repo-relative).
- Report fixes: move numbers printed as chess notation with color marker
  (`10. Qd2` / `13... bxa3`) instead of raw `ply + 1`; missed-wins entries gain a
  `move` field (raw `ply` kept for compatibility); "плохих ходов" column name
  (was "ходов"); "Цейтнот: нет данных" instead of a misleading "(0%)" when no
  time-pressure moves were played (`tp_blunder_rate_pct` is now `None` then);
  average win% loss/before in report and plan now skip games with missing values
  instead of substituting 0.
- `app/plan.py`: branch entries gain explicit `path_ok_rate` (line strength,
  i.e. path-wide); the legacy `ok_rate` key is kept and equals it.
- `app/repertoire.py`: weak-opening rows now encode `color` explicitly as
  `white`/`black`/`mixed` (`mixed` = both colors aggregated in one row), with
  `white_games`/`black_games` counters; previously a silent "majority color" label.
- `app/opening_book.py`: main-line tie-break comment/code aligned with the
  module's frequency-then-SAN rule.
- `app/mistakes.py`: dead `TIME_PRESSURE_CLOCK`/`TIME_PRESSURE_DROP` removed
  (time pressure is marked in `app.analyzer` by < 30.0 s clock).
- `app/accuracy.py`: docstring corrected — tactics without motifs is `None`, not 0.
- `app/games.py`: a nick matching neither player now logs a WARNING instead of
  silently attributing moves to white; still falls back to white because all
  downstream consumers require a binary color — pass `user_color` explicitly when
  importing PGNs.
- New `tests/test_release_hygiene.py` covers the CLI/report fixes: chess-notation
  move labels, missing-metric handling (no fake 0), time-pressure "нет данных",
  плохих ходов header, `path_ok_rate`/`ok_rate` compatibility, opening-stats color
  encoding, user-color fallback warning, and the opening-book tie-break rule.
- Frontend asset URLs bumped to `?v=0.9.22` (cache-busting only).
- Test condition: 1069 pass with `LLM_API_KEY=test LLM_MODEL=openai/gpt-4o-mini`;
  without the key, the 6 middlegame API tests listed above fail.

## 0.9.21 — 2026-11-08

- Added a real opening repertoire built from your own games: first move,
  most frequent opponent reply, your response and the main full line.
  Example: 1. e4 e6 2. d4 d5 3. Nd2 is now shown as a real played line.
- No LLM is used to invent repertoire moves; counts come from cached games.
- 1049 tests pass.

## 0.9.20 — 2026-11-07

- New deterministic move themes in the coach report: `tactics`, `positional`
  and `attack` are classified from Stockfish multipv/PV, position facts and
  the played move, not from an LLM.
- Reports now show opportunity/outcome counts, missed examples, average
  win% loss and excluded ambiguous positions; insufficient samples are shown
  as “недостаточно данных”, not fake 0% accuracy.
- Old cached analyses without theme annotations remain readable.
- 1046 tests pass.

## 0.9.19 — 2026-11-05

- Reading master tasks now show the author's intended move as the correct
  answer and Stockfish's best move separately as "Компьютер".
- Reading course answers were corrected to engine-best alternatives; factual
  why text now cites Stockfish's move and line.
- 1032 tests pass.

## 0.9.18 — 2026-11-05

- Endgame master tasks now keep the author's intended move as the correct
  answer, while separately showing Stockfish's computer-best move and line.
- 931 tests pass.

## 0.9.17 — 2026-11-05

- Reading course answers corrected to engine-best moves. Accepted moves are now
  only moves within ~20 cp of Stockfish depth 14; every best move and line was
  revalidated with a fresh engine.
- 1032 tests pass.

## 0.9.16 — 2026-11-05

- New “Моя система” reading trainer in the nav: 6 original chapters based on
  public-domain My System ideas, validated FEN/answer moves, no text copied,
  and integrated lesson/answer flow with progress and review.
- Add backend reading APIs, attempts storage, and full frontend catalog/board
  UI; reset stale-cache behavior via version bump after ship.
- 1032 tests pass.

## 0.9.15 — 2026-11-04

- New “Эндшпиль” trainer in the web app: choose between master endgame positions and endgame mistakes from your own analyzed games. Master tasks are curated theory (Lucena, Philidor, opposition, triangulation, key squares, active king, fortress, pawn race, rook exchange, mate net). Own tasks are derived only from cached analysis (blunder/mistake, phase=endgame) and show the game/ply with an “Открыть партию” button.
- Answer endpoint validates SAN/UCI, returns the solution/line and optional engine/tablebase verdict; optional `SYZYGY_PATH` enables Syzygy tables, otherwise Stockfish is used; missing path never breaks the API.
- Progress is stored per user in meta and shown in the trainer header.
- Backend tests added; frontend uses the existing board and opens the exact ply in the game view.
- 931 tests pass.

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
