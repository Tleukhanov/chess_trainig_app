// Browser-side Stockfish client.
//
// Wraps the vendored Stockfish Web Worker (frontend/vendor/stockfish/stockfish.js)
// and exposes a small promise-based API for analysing a position. No build
// tooling is required: this is a plain ES module that imports the vendored
// chess.js helper for UCI -> SAN conversion.
//
// Usage:
//   import { createStockfishClient, analyzePosition, stopClient } from './stockfish-client.js';
//   const client = createStockfishClient({ depth: 14, multiPv: 3 });
//   const result = await analyzePosition(client, 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1');
//   stopClient(client);

import { Chess } from '../vendor/chess/chess.js';

const DEFAULT_OPTIONS = {
  depth: 12,
  multiPv: 3,
  movetime: null,
  timeoutMs: 15000,
  threads: 1,
  hash: 64,
};

let defaultClient = null;

function mergeOptions(base, extra) {
  return Object.assign({}, DEFAULT_OPTIONS, base || {}, extra || {});
}

function normalizeMessage(data) {
  if (typeof data === 'string') return data;
  if (data && typeof data.data === 'string') return data.data;
  try { return String(data); } catch (e) { return ''; }
}

function parseInfoLine(line) {
  if (typeof line !== 'string' || !line.startsWith('info')) return null;
  const tokens = line.trim().split(/\s+/);
  const indexOf = (key) => tokens.indexOf(key);
  const valueAfter = (key) => {
    const i = indexOf(key);
    return i >= 0 && i + 1 < tokens.length ? tokens[i + 1] : null;
  };
  const multipv = parseInt(valueAfter('multipv') || '1', 10) || 1;
  const depth = parseInt(valueAfter('depth') || '0', 10) || 0;
  const seldepth = parseInt(valueAfter('seldepth') || '0', 10) || 0;
  let scoreType = null;
  let scoreValue = null;
  const scoreIndex = indexOf('score');
  if (scoreIndex >= 0) {
    scoreType = tokens[scoreIndex + 1] || null;
    const raw = tokens[scoreIndex + 2];
    const parsed = parseInt(raw, 10);
    scoreValue = Number.isNaN(parsed) ? null : parsed;
  }
  const pvIndex = indexOf('pv');
  const pv = pvIndex >= 0 ? tokens.slice(pvIndex + 1) : [];
  return { multipv, depth, seldepth, scoreType, scoreValue, pv, raw: line };
}

function parseBestMoveLine(line) {
  if (typeof line !== 'string' || !line.startsWith('bestmove')) return null;
  const parts = line.trim().split(/\s+/);
  if (parts.length < 2 || !parts[1] || parts[1] === '(none)') return null;
  return parts[1];
}

function uciToSan(fen, uciMoves) {
  const sans = [];
  let chess;
  try {
    chess = new Chess(fen);
  } catch (e) {
    return uciMoves.slice();
  }
  for (const uci of uciMoves) {
    if (typeof uci !== 'string' || uci.length < 4) {
      sans.push(uci);
      break;
    }
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const promotion = uci.length > 4 ? uci.slice(4, 5) : undefined;
    let move = null;
    try {
      move = chess.move({ from, to, promotion: promotion || undefined });
    } catch (e) {
      move = null;
    }
    if (!move) {
      sans.push(uci);
      break;
    }
    sans.push(move.san);
  }
  return sans;
}

function scoreToWinPercent(scoreType, scoreValue) {
  if (scoreType === 'mate') {
    if (scoreValue === null || scoreValue === 0) return 50;
    return scoreValue > 0 ? 100 : 0;
  }
  const cp = (typeof scoreValue === 'number') ? scoreValue : 0;
  // Logistic win probability for the side to move (Lichess-style constant).
  const probability = 2 / (1 + Math.exp(-0.00368208 * cp));
  return 50 + 50 * probability;
}

function infoToLine(info, fen) {
  const uci = (info.pv || []).slice();
  const san = uciToSan(fen, uci);
  const scoreType = info.scoreType;
  const scoreValue = info.scoreValue;
  let evalPawns;
  if (scoreType === 'mate') {
    evalPawns = scoreValue > 0 ? 100 : -100;
  } else if (scoreType === 'cp') {
    evalPawns = (scoreValue || 0) / 100;
  } else {
    evalPawns = 0;
  }
  return {
    multipv: info.multipv,
    depth: info.depth,
    seldepth: info.seldepth,
    scoreType: scoreType,
    score: scoreValue,
    eval: evalPawns,
    winPercent: scoreToWinPercent(scoreType, scoreValue),
    pv: uci.slice(),
    uci: uci.slice(),
    san: san.slice(),
    line: san.join(' '),
    raw: info.raw,
  };
}

function validateFen(fen) {
  try {
    // Chess constructor throws on illegal FEN.
    new Chess(fen);
    return true;
  } catch (e) {
    return false;
  }
}

function createMessageBus(client) {
  client._listeners = new Set();
  client.worker.onmessage = (event) => {
    const line = normalizeMessage(event.data);
    client._listeners.forEach((cb) => {
      try { cb(line, null); } catch (e) { /* swallow listener errors */ }
    });
  };
  client.worker.onerror = (event) => {
    const err = (event && event.message) ? new Error(event.message) : new Error('Stockfish worker error');
    client._listeners.forEach((cb) => {
      try { cb(null, err); } catch (e) { /* swallow */ }
    });
  };
}

function waitForLine(client, predicate, timeoutMs) {
  return new Promise((resolve, reject) => {
    let timer = null;
    const cb = (line, err) => {
      if (err) {
        cleanup();
        reject(err);
        return;
      }
      if (line !== null && predicate(line)) {
        cleanup();
        resolve(line);
      }
    };
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      client._listeners.delete(cb);
    };
    client._listeners.add(cb);
    timer = setTimeout(() => {
      cleanup();
      reject(new Error('Stockfish timeout waiting for engine response'));
    }, timeoutMs);
  });
}

function send(client, command) {
  if (!client || !client.worker || client.destroyed) {
    throw new Error('Stockfish client is not available');
  }
  client.worker.postMessage(command);
}

async function initClient(client) {
  const opts = client.options;
  const timeoutMs = opts.timeoutMs;
  send(client, 'uci');
  await waitForLine(client, (line) => line.trim() === 'uciok', timeoutMs);
  send(client, `setoption name Threads value ${opts.threads}`);
  send(client, `setoption name Hash value ${opts.hash}`);
  send(client, `setoption name MultiPV value ${opts.multiPv}`);
  send(client, 'isready');
  await waitForLine(client, (line) => line.trim() === 'readyok', timeoutMs);
  send(client, 'ucinewgame');
  client.ready = true;
  return client;
}

function enqueue(client, task) {
  const previous = client._taskQueue || Promise.resolve();
  let resolveOuter;
  let rejectOuter;
  const resultPromise = new Promise((res, rej) => {
    resolveOuter = res;
    rejectOuter = rej;
  });
  client._taskQueue = previous.then(
    () => task().then(resolveOuter, rejectOuter),
    () => task().then(resolveOuter, rejectOuter)
  );
  return resultPromise;
}

export function createStockfishClient(options = {}) {
  const opts = mergeOptions({}, options);
  let worker;
  try {
    worker = new Worker(new URL('../vendor/stockfish/stockfish.js', import.meta.url));
  } catch (e) {
    throw new Error('Unable to create Stockfish worker: ' + (e && e.message ? e.message : e));
  }
  const client = {
    id: 'stockfish-' + Math.random().toString(36).slice(2),
    options: opts,
    worker: worker,
    ready: false,
    destroyed: false,
    _multiPv: opts.multiPv,
    _taskQueue: Promise.resolve(),
  };
  createMessageBus(client);
  client.readyPromise = initClient(client).catch((err) => {
    client.ready = false;
    throw err;
  });
  if (defaultClient === null) defaultClient = client;
  return client;
}

// Contract: export function analyzePosition (async implementation)
export async function analyzePosition(clientOrFen, fenOrOptions, options) {
  let client;
  let fen;
  let extra = {};
  if (typeof clientOrFen === 'string') {
    fen = clientOrFen;
    extra = (fenOrOptions && typeof fenOrOptions === 'object') ? fenOrOptions : {};
    if (!defaultClient || defaultClient.destroyed) {
      defaultClient = createStockfishClient(extra);
    }
    client = defaultClient;
  } else {
    client = clientOrFen;
    fen = fenOrOptions;
    extra = (options && typeof options === 'object') ? options : {};
  }

  if (!client || client.destroyed) {
    throw new Error('Stockfish client is destroyed');
  }
  if (typeof fen !== 'string' || !fen.trim()) {
    throw new Error('A FEN string is required');
  }
  fen = fen.trim();
  if (!validateFen(fen)) {
    throw new Error('Invalid FEN: ' + fen);
  }

  const task = async () => {
    await client.readyPromise;
    const opts = mergeOptions(client.options, extra);
    const depth = (typeof extra.depth === 'number') ? extra.depth : opts.depth;
    const multiPv = (typeof extra.multiPv === 'number') ? extra.multiPv
      : (typeof extra.multipv === 'number') ? extra.multipv : opts.multiPv;
    const movetime = (typeof extra.movetime === 'number') ? extra.movetime
      : (typeof extra.moveTime === 'number') ? extra.moveTime : opts.movetime;
    const timeoutMs = (typeof extra.timeoutMs === 'number') ? extra.timeoutMs : opts.timeoutMs;

    // Update MultiPV if needed.
    if (multiPv !== client._multiPv) {
      send(client, `setoption name MultiPV value ${multiPv}`);
      send(client, 'isready');
      await waitForLine(client, (line) => line.trim() === 'readyok', timeoutMs);
      client._multiPv = multiPv;
    }

    const linesMap = new Map();
    let bestMove = null;
    let currentDepth = depth;
    let finished = false;
    let graceTimer = null;

    const result = await new Promise((resolve, reject) => {
      let timer = null;
      const finish = (value, err) => {
        if (finished) return;
        finished = true;
        if (timer) clearTimeout(timer);
        if (graceTimer) clearTimeout(graceTimer);
        client._listeners.delete(listener);
        if (err) reject(err); else resolve(value);
      };

      const listener = (line, err) => {
        if (finished) return;
        if (err) {
          finish(null, err);
          return;
        }
        if (!line) return;
        const info = parseInfoLine(line);
        if (info) {
          linesMap.set(info.multipv, info);
          if (info.depth) currentDepth = Math.max(currentDepth, info.depth);
        }
        const bm = parseBestMoveLine(line);
        if (bm) {
          bestMove = bm;
          const sorted = [...linesMap.values()].sort((a, b) => a.multipv - b.multipv);
          const lines = sorted.map((info) => infoToLine(info, fen));
          const bestMoveSan = uciToSan(fen, [bestMove])[0] || bestMove;
          const primary = lines.length ? lines[0] : null;
          const payload = {
            fen: fen,
            depth: currentDepth,
            bestMove: bestMove,
            bestMoveUci: bestMove,
            bestMoveSan: bestMoveSan,
            lines: lines,
            multipv: lines.length,
            uci: bestMove,
            san: bestMoveSan,
            eval: primary ? primary.eval : 0,
            winPercent: primary ? primary.winPercent : 50,
            rawBestMove: line,
          };
          finish(payload, null);
        }
      };

      client._listeners.add(listener);

      timer = setTimeout(() => {
        try { send(client, 'stop'); } catch (e) { /* ignore */ }
        graceTimer = setTimeout(() => {
          finish(null, new Error('Stockfish analysis timeout after ' + timeoutMs + 'ms'));
        }, 1000);
      }, timeoutMs);

      try {
        send(client, `position fen ${fen}`);
        if (movetime && movetime > 0) {
          send(client, `go movetime ${movetime}`);
        } else {
          send(client, 'go depth ' + depth);
        }
      } catch (e) {
        finish(null, e);
      }
    });

    return result;
  };

  return enqueue(client, task);
}

export function stopClient(client) {
  const target = client || defaultClient;
  if (!target) return;
  target.destroyed = true;
  try {
    if (target.worker) target.worker.postMessage('quit');
  } catch (e) { /* ignore */ }
  try {
    if (target.worker) target.worker.terminate();
  } catch (e) { /* ignore */ }
  target._listeners = new Set();
  if (target === defaultClient) defaultClient = null;
}

export default {
  createStockfishClient,
  analyzePosition,
  stopClient,
};
