# Browser chess engine client

This directory contains the browser-side Stockfish client used for local
analysis of a position (no server round-trip).

## Files

- `stockfish-client.js` - ES module exporting `createStockfishClient`,
  `analyzePosition` and `stopClient`. Runs the vendored Stockfish Web Worker
  (`../vendor/stockfish/stockfish.js`) using the UCI protocol and converts
  principal variations to SAN via `../vendor/chess/chess.js`.
- `../vendor/stockfish/` - vendored Stockfish (GPL-3.0, see COPYING.txt).
- `../vendor/chess/` - vendored chess.js (BSD-2-Clause, see LICENSE).

## Usage

```js
import { createStockfishClient, analyzePosition, stopClient } from './stockfish-client.js';

const client = createStockfishClient({ depth: 14, multiPv: 3 });
const result = await analyzePosition(client, 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1');
console.log(result.bestMoveSan, result.lines);
stopClient(client);
```

`analyzePosition` resolves with `{ fen, depth, bestMove, bestMoveSan, lines,
multipv, eval, winPercent }`, where each line has `multipv, depth, score,
eval, winPercent, pv (UCI), uci, san, line`.

## Notes

- No build tooling is required; the module uses standard ES module imports.
- The worker is terminated by `stopClient`. Analysis calls are serialized per
  client and time out after `timeoutMs`.
