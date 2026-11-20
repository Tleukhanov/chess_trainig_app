// Browser-side local game analysis.
//
// Replays a game with chess.js, asks a single Stockfish (WASM) worker to
// evaluate every position, and builds an object that is compatible with the
// shape produced by ``GameAnalysis.summary()`` on the server (see
// app/analyzer.py). Extra client-only fields (analysis_source, engine, depth,
// multipv, client_lines) are added so callers can tell local and server
// analyses apart.
//
// Usage:
//   import { winPercent, classifyMove, analyzeGameLocally } from './local-analysis.js';
//   const summary = await analyzeGameLocally(gameOrMovesOrPgn, {
//     depth: 12, multipv: 3, onProgress: (p) => console.log(p),
//   });

import { createStockfishClient, analyzePosition, stopClient } from './stockfish-client.js';
import { Chess } from '../vendor/chess/chess.js';

const MAX_ANALYZE_PLIES = 120;

export function winPercent(cpCentipawns, mate = null) {
  if (mate !== null && mate !== undefined) {
    let mateValue = 0;
    try { mateValue = parseInt(mate, 10); } catch (e) { mateValue = 0; }
    if (Number.isNaN(mateValue)) mateValue = 0;
    if (mateValue > 0) return 100.0;
    if (mateValue < 0) return 0.0;
    return 50.0;
  }
  let cpValue;
  try { cpValue = parseFloat(cpCentipawns); } catch (e) { cpValue = NaN; }
  if (Number.isNaN(cpValue)) return 50.0;
  if (cpValue === Infinity) return 100.0;
  if (cpValue === -Infinity) return 0.0;
  cpValue = Math.max(-10000.0, Math.min(10000.0, cpValue));
  try {
    return 50.0 + 50.0 * (2.0 / (1.0 + Math.exp(-0.00368208 * cpValue)) - 1.0);
  } catch (e) {
    return cpValue < 0 ? 0.0 : 100.0;
  }
}

export function classifyMove(winBefore, winAfter, isBest = false) {
  if (winBefore === null || winBefore === undefined ||
      winAfter === null || winAfter === undefined) {
    return 'good';
  }
  const wb = Number(winBefore);
  const wa = Number(winAfter);
  if (Number.isNaN(wb) || Number.isNaN(wa)) return 'good';
  if (wb >= 99.9 && wa < 95.0) return 'blunder';
  const drop = wb - wa;
  if (isBest && drop < 2.0) return 'best';
  if (drop >= 20.0) return 'blunder';
  if (drop >= 10.0) return 'mistake';
  if (drop >= 5.0) return 'inaccuracy';
  return 'good';
}

function round2(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  if (Number.isNaN(n)) return null;
  return Math.round(n * 100) / 100;
}

function sanKey(san) {
  return String(san || '').replace(/[+#]+$/g, '');
}

function normalizeInput(input, options) {
  const opts = options || {};
  let moves = [];
  let clocks = opts.clocks !== undefined ? opts.clocks : (opts.clock !== undefined ? opts.clock : null);
  let userColor = opts.user_color || opts.userColor || 'white';
  let gameId = opts.id || opts.game_id || opts.gameId || null;
  let opponent = opts.opponent || null;
  let resultForUser = opts.result_for_user || opts.resultForUser || null;

  if (Array.isArray(input)) {
    moves = input.slice();
  } else if (typeof input === 'string') {
    const trimmed = input.trim();
    if (!trimmed) {
      moves = [];
    } else {
      try {
        const chess = new Chess();
        chess.loadPgn(trimmed);
        moves = chess.history();
        const headers = (typeof chess.header === 'function') ? chess.header() : null;
        if (headers && typeof headers === 'object') {
          if (headers.White && headers.Black && !opts.user_color && !opts.userColor) {
            // Keep default; caller can override via options.
          }
        }
      } catch (e) {
        moves = trimmed.split(/\s+/).filter(Boolean);
      }
    }
  } else if (input && typeof input === 'object') {
    if (Array.isArray(input.moves)) {
      moves = input.moves.slice();
    } else if (typeof input.moves === 'string') {
      moves = input.moves.trim().split(/\s+/).filter(Boolean);
    } else if (Array.isArray(input.sans)) {
      moves = input.sans.slice();
    }
    if (input.clocks !== undefined) clocks = input.clocks;
    else if (input.clock !== undefined) clocks = input.clock;
    if (input.user_color || input.userColor) userColor = input.user_color || input.userColor;
    if (input.id || input.game_id || input.gameId) gameId = input.id || input.game_id || input.gameId;
    if (input.opponent !== undefined) opponent = input.opponent;
    if (input.result_for_user || input.resultForUser) resultForUser = input.result_for_user || input.resultForUser;
  }

  if (!Array.isArray(clocks)) {
    if (typeof clocks === 'string') {
      clocks = clocks.trim().split(/\s+/).filter(Boolean).map((v) => {
        const n = parseFloat(v);
        return Number.isNaN(n) ? null : n;
      });
    } else {
      clocks = [];
    }
  } else {
    clocks = clocks.map((v) => {
      if (v === null || v === undefined) return null;
      const n = (typeof v === 'number') ? v : parseFloat(v);
      return Number.isNaN(n) ? null : n;
    });
  }

  if (userColor !== 'white' && userColor !== 'black') userColor = 'white';

  return {
    moves,
    clocks,
    user_color: userColor,
    id: gameId,
    opponent: opponent,
    result_for_user: resultForUser,
  };
}

function extractEval(result) {
  // Returns { cp (centipawns or null), mate (moves or null) } for side to move.
  const lines = Array.isArray(result && result.lines) ? result.lines : [];
  const primary = lines.length ? lines[0] : null;
  if (primary) {
    const scoreType = primary.scoreType || primary.score_type;
    const score = (primary.score !== undefined && primary.score !== null) ? primary.score
      : (primary.scoreValue !== undefined ? primary.scoreValue : primary.score_value);
    if (scoreType === 'mate') {
      const mv = (typeof score === 'number') ? score : parseInt(score, 10);
      return { cp: null, mate: Number.isNaN(mv) ? null : mv };
    }
    if (typeof score === 'number') {
      return { cp: score, mate: null };
    }
  }
  // Contract-style: result.eval may be {cp, mate}.
  const ev = result && result.eval;
  if (ev && typeof ev === 'object' && !Array.isArray(ev)) {
    if (ev.mate !== undefined && ev.mate !== null) {
      const mv = (typeof ev.mate === 'number') ? ev.mate : parseInt(ev.mate, 10);
      return { cp: null, mate: Number.isNaN(mv) ? null : mv };
    }
    if (ev.cp !== undefined && ev.cp !== null) {
      const n = Number(ev.cp);
      if (!Number.isNaN(n)) {
        // If fractional (pawns), scale to centipawns.
        if (!Number.isInteger(n) && Math.abs(n) <= 100) {
          return { cp: n * 100, mate: null };
        }
        return { cp: n, mate: null };
      }
    }
  }
  // Top-level numeric eval (Agent A): pawns -> centipawns.
  if (typeof ev === 'number' && !Number.isNaN(ev)) {
    if (!Number.isInteger(ev) && Math.abs(ev) <= 100) {
      return { cp: ev * 100, mate: null };
    }
    // Could already be centipawns.
    if (Math.abs(ev) > 100) {
      return { cp: ev, mate: null };
    }
    // Ambiguous small integer: prefer centipawns if lines absent.
    return { cp: ev, mate: null };
  }
  return { cp: 0, mate: null };
}

function getLines(result) {
  if (Array.isArray(result && result.lines)) return result.lines;
  return [];
}

function getBestMoveSan(result, fen) {
  if (result && (result.bestmoveSan || result.bestMoveSan || result.best_move_san || result.san)) {
    return result.bestmoveSan || result.bestMoveSan || result.best_move_san || result.san || null;
  }
  const lines = getLines(result);
  if (lines.length) {
    const l = lines[0];
    if (Array.isArray(l.san) && l.san.length) return l.san[0];
    if (typeof l.line === 'string' && l.line.trim()) return l.line.trim().split(/\s+/)[0];
  }
  return null;
}

function getFullPvSan(result) {
  const lines = getLines(result);
  if (lines.length) {
    const l = lines[0];
    if (Array.isArray(l.san) && l.san.length) return l.san.slice();
    if (Array.isArray(l.pvSan) && l.pvSan.length) return l.pvSan.slice();
    if (Array.isArray(l.pv_san) && l.pv_san.length) return l.pv_san.slice();
  }
  if (Array.isArray(result && result.pvSan)) return result.pvSan.slice();
  if (Array.isArray(result && result.pv_san)) return result.pv_san.slice();
  if (Array.isArray(result && result.pvUci)) {
    // fallback: return uci as strings
    return result.pvUci.slice();
  }
  return [];
}

function getFullPvUci(result) {
  const lines = getLines(result);
  if (lines.length) {
    const l = lines[0];
    if (Array.isArray(l.uci) && l.uci.length) return l.uci.slice();
    if (Array.isArray(l.pv) && l.pv.length) return l.pv.slice();
  }
  if (Array.isArray(result && result.pvUci)) return result.pvUci.slice();
  if (Array.isArray(result && result.pv_uci)) return result.pv_uci.slice();
  return [];
}

export async function analyzeGameLocally(input, options = {}) {
  const opts = options || {};
  const game = normalizeInput(input, opts);
  const depth = (typeof opts.depth === 'number') ? opts.depth : (typeof opts.Depth === 'number' ? opts.Depth : 12);
  const multipv = (typeof opts.multipv === 'number') ? opts.multipv
    : (typeof opts.multiPv === 'number') ? opts.multiPv
    : (typeof opts.multiPV === 'number') ? opts.multiPV : 3;
  const onProgress = (typeof opts.onProgress === 'function') ? opts.onProgress : null;

  const moves = (game.moves || []).slice(0, MAX_ANALYZE_PLIES);
  const clocks = game.clocks || [];
  const userColor = game.user_color || 'white';
  const userIsWhite = userColor === 'white';

  let client = null;
  try {
    client = createStockfishClient({ depth, multiPv: multipv, timeoutMs: opts.timeoutMs });

    // Analyze each position in sequence (single worker client).
    const entries = [];

    // Reconstruct board positions for every ply up to the first illegal move.
    const boards = [new Chess()];
    for (let i = 0; i < moves.length; i++) {
      const b = new Chess();
      // clone via fen
      try {
        b.load(boards[i].fen());
      } catch (e) {
        // ignore
      }
      try {
        b.move(moves[i]);
      } catch (e) {
        // illegal move; stop
        break;
      }
      boards.push(b);
    }
    // Now boards[ply] is position before move ply (if moves legal up to ply).
    // But if break early, boards length < moves+1; handle.

    for (let ply = 0; ply <= moves.length; ply++) {
      if (ply >= boards.length) break;
      const b = boards[ply];
      let positionEntries;
      if (b.isGameOver()) {
        if (b.isCheckmate()) {
          // side to move gets mated
          positionEntries = {
            beforeEval: { cp: null, mate: -1 },
            bestMoveSan: null,
            fullPv: [],
            lines: [],
          };
        } else {
          positionEntries = {
            beforeEval: { cp: 0, mate: null },
            bestMoveSan: null,
            fullPv: [],
            lines: [],
          };
        }
        entries.push(positionEntries);
        if (ply < moves.length) break;
        continue;
      }
      if (onProgress) {
        try { onProgress({ ply, total: moves.length, current: ply + 1 }); } catch (e) { /* ignore */ }
      }
      const fen = b.fen();
      const result = await analyzePosition(client, fen, { depth, multiPv: multipv });
      const ev = extractEval(result);
      entries.push({
        beforeEval: ev,
        bestMoveSan: getBestMoveSan(result, fen),
        fullPv: getFullPvSan(result),
        lines: getLines(result),
        result,
      });
      // Stop if we've reached end
    }

    // Now build per-ply analyses
    const analyses = [];
    for (let ply = 0; ply < moves.length; ply++) {
      if (ply + 1 >= entries.length) break;
      const before = entries[ply];
      const afterEntry = entries[ply + 1];
      const beforeEval = before.beforeEval;
      const afterSideEval = afterEntry.beforeEval;

      const winBeforeRaw = winPercent(beforeEval.cp, beforeEval.mate);
      const winAfterSideRaw = winPercent(afterSideEval.cp, afterSideEval.mate);
      const winBefore = round2(winBeforeRaw);
      const winAfter = round2(100.0 - winAfterSideRaw);
      const drop = round2(winBeforeRaw - (100.0 - winAfterSideRaw));

      const isBest = before.bestMoveSan != null && sanKey(before.bestMoveSan) === sanKey(moves[ply]);
      const classification = classifyMove(winBeforeRaw, 100.0 - winAfterSideRaw, isBest);

      // flip after eval for mover perspective
      const afterEval = {
        cp: (afterSideEval.cp === null ? null : -afterSideEval.cp),
        mate: (afterSideEval.mate === null ? null : -afterSideEval.mate),
      };

      // clock handling
      let clockUsed = null;
      let timePressure = null;
      if (ply < clocks.length) {
        const current = clocks[ply];
        if (current !== null && current !== undefined) {
          timePressure = current < 30.0;
          if (ply >= 2 && ply - 2 < clocks.length) {
            const prev = clocks[ply - 2];
            if (prev !== null && prev !== undefined) {
              clockUsed = Math.max(0.0, prev - current);
            }
          }
        }
      }

      const bestLine = Array.isArray(before.fullPv) ? before.fullPv.slice(1, 7) : [];
      const playedLine = moves.slice(ply + 1, ply + 1 + 5);

      const moveAnalysis = {
        san: moves[ply],
        before: { cp: beforeEval.cp, mate: beforeEval.mate },
        after: { cp: afterEval.cp, mate: afterEval.mate },
        win_before: winBefore,
        win_after: winAfter,
        drop: drop,
        classification: classification,
        best_move_san: before.bestMoveSan || null,
        best_eval: { cp: beforeEval.cp, mate: beforeEval.mate },
        best_win: winBefore,
        clock_used: clockUsed,
        time_pressure: timePressure,
        best_line: bestLine,
        played_line: playedLine,
        client_lines: before.lines || [],
      };
      analyses.push(moveAnalysis);
    }

    // Aggregates over user moves.
    const userIsWhiteLocal = userIsWhite;
    const userIndices = analyses.map((_, i) => i).filter((i) => ((i % 2 === 0) === userIsWhiteLocal));

    const lossValues = userIndices.map((i) => analyses[i].drop).filter((v) => v !== null && v !== undefined);
    const avgWinLoss = lossValues.length ? round2(lossValues.reduce((a, b) => a + b, 0) / lossValues.length) : 0.0;

    const beforeValues = userIndices.map((i) => analyses[i].win_before).filter((v) => v !== null && v !== undefined);
    const avgWinBefore = beforeValues.length ? round2(beforeValues.reduce((a, b) => a + b, 0) / beforeValues.length) : 0.0;

    const blunders = userIndices.filter((i) => analyses[i].classification === 'blunder');
    const mistakes = userIndices.filter((i) => analyses[i].classification === 'mistake');
    const inaccuracies = userIndices.filter((i) => analyses[i].classification === 'inaccuracy');
    const missedWins = userIndices.filter((i) => {
      const m = analyses[i];
      return m.win_before !== null && m.win_before >= 95.0 &&
             m.win_after !== null && m.win_after <= 90.0;
    });
    const timePressureBlunders = blunders.filter((i) => analyses[i].time_pressure === true);

    const summary = {
      game_id: game.id || null,
      user_color: userColor,
      result_for_user: game.result_for_user || null,
      opponent: game.opponent || null,
      avg_win_loss: avgWinLoss,
      avg_win_before: avgWinBefore,
      blunders: blunders,
      mistakes: mistakes,
      inaccuracies: inaccuracies,
      missed_wins: missedWins,
      time_pressure_blunders: timePressureBlunders,
      moves: analyses,
      analysis_source: 'client',
      engine: 'stockfish-wasm',
      depth: depth,
      multipv: multipv,
    };
    return summary;
  } catch (err) {
    const message = (err && err.message) ? err.message : String(err);
    throw new Error('Local analysis failed: ' + message);
  } finally {
    if (client) {
      try { stopClient(client); } catch (e) { /* ignore */ }
    }
  }
}

export default {
  winPercent,
  classifyMove,
  analyzeGameLocally,
};
