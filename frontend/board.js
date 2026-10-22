// Interactive chessboard in the spirit of Lichess:
// — cburnett SVG pieces (the default Lichess set),
// — click or drag a piece, legal destinations show dots (rings for captures),
// — right-button drag draws arrows, right-click highlights a square,
// — last move slides into place with a short animation.
//
// The module is framework-free: boardHtml() renders markup, bindBoard() wires
// pointer handlers after the markup is inserted into the DOM.

const FILES = 'abcdefgh';

const PIECE_IMAGES = {
  K: 'wK', Q: 'wQ', R: 'wR', B: 'wB', N: 'wN', P: 'wP',
  k: 'bK', q: 'bQ', r: 'bR', b: 'bB', n: 'bN', p: 'bP',
};

const PIECE_DIR = 'assets/pieces/cburnett';
const SVG_NS = 'http://www.w3.org/2000/svg';

export const PIECE_SET = PIECE_DIR;

export function fenPieces(fen) {
  const placement = String(fen || '').split(' ')[0];
  const pieces = {};
  let rank = 8;
  let file = 0;
  for (const ch of placement) {
    if (ch === '/') { rank--; file = 0; continue; }
    if (ch >= '1' && ch <= '8') { file += +ch; continue; }
    pieces[FILES[file] + rank] = ch;
    file++;
  }
  return pieces;
}

export function lastMoveSquares(fenBefore, fenAfter) {
  const before = fenPieces(fenBefore);
  const after = fenPieces(fenAfter);
  const froms = [];
  const tos = [];
  const changed = [];
  for (let r = 8; r >= 1; r--) {
    for (let f = 0; f < 8; f++) {
      const sq = FILES[f] + r;
      if (before[sq] && !after[sq]) froms.push(sq);
      if (!before[sq] && after[sq]) tos.push(sq);
      if (before[sq] && after[sq] && before[sq] !== after[sq]) changed.push(sq);
    }
  }
  if (froms.length === 2 && tos.length === 1) {
    // en passant: снятая пешка стоит на вертикали пункта назначения
    const to = tos[0];
    const captured = froms.find(s => s[0] === to[0]) || froms[1];
    const from = froms.find(s => s !== captured) || froms[0];
    return [from, to, captured];
  }
  // взятие: поле назначения было занято, поэтому состав фигур там просто сменился
  if (froms.length === 1 && tos.length === 0 && changed.length === 1) return [froms[0], changed[0]];
  // обычный ход: одно поле опустело, другое заполнилось
  if (froms.length === 1 && tos.length === 1) return [froms[0], tos[0]];
  return froms.concat(tos);
}

function squarePoint(square, flip) {
  const file = FILES.indexOf(square[0]);
  const rank = Number(square[1]);
  if (file < 0 || !rank) return null;
  const col = flip ? 7 - file : file;
  const row = flip ? rank - 1 : 8 - rank;
  return { x: col + 0.5, y: row + 0.5 };
}

/**
 * Render a board.
 * opts.lastMove  — squares to highlight as the last move (array of algebraic names)
 * opts.flip      — black at the bottom
 * opts.animFrom  — square the moving piece comes from (animation start)
 * opts.animTo    — destination square (animation end)
 * opts.animate   — set to false to render without the slide-in animation
 */
export function boardHtml(fen, opts = {}) {
  const pieces = fenPieces(fen);
  const lastMove = Array.isArray(opts.lastMove) ? opts.lastMove : [];
  const flip = !!opts.flip;

  const animFrom = opts.animFrom || (lastMove.length === 2 ? lastMove[0] : '');
  const animTo = opts.animTo || (lastMove.length === 2 ? lastMove[1] : '');
  const animate = opts.animate !== false && animFrom && animTo && animFrom !== animTo;

  const ranks = [];
  for (let r = 8; r >= 1; r--) ranks.push(r);
  if (flip) ranks.reverse();
  const files = [];
  for (let f = 0; f < 8; f++) files.push(f);
  if (flip) files.reverse();

  let html = '';
  for (let ri = 0; ri < 8; ri++) {
    const rank = ranks[ri];
    for (let ci = 0; ci < 8; ci++) {
      const f = files[ci];
      const square = FILES[f] + rank;
      const light = ((f + (8 - rank)) % 2 === 0);
      const cls = ['sq', light ? 'light' : 'dark'];
      if (lastMove.includes(square)) cls.push('lm');
      if (opts.check === square) cls.push('in-check');
      const piece = pieces[square];
      const coordColor = light ? 'var(--c-sq-dark)' : 'var(--c-sq-light)';
      let coord = '';
      if (ci === 0) coord += `<span class="board-coord board-coord-rank" style="color:${coordColor}">${rank}</span>`;
      if (ri === 7) coord += `<span class="board-coord board-coord-file" style="color:${coordColor}">${FILES[f]}</span>`;
      const pieceHtml = piece
        ? `<span class="piece ${piece === piece.toUpperCase() ? 'pw' : 'pb'}"><img src="${PIECE_DIR}/${PIECE_IMAGES[piece]}.svg" alt="" draggable="false" decoding="async"></span>`
        : '';
      html += `<div class="${cls.join(' ')}" data-square="${square}">${coord}${pieceHtml}</div>`;
    }
  }

  return `<div class="board${flip ? ' flipped' : ''}" data-flip="${flip ? '1' : '0'}"
    data-anim-from="${animate ? animFrom : ''}" data-anim-to="${animate ? animTo : ''}">
    <div class="board-grid">${html}</div>
    <svg class="board-arrows" viewBox="0 0 8 8" preserveAspectRatio="none" aria-hidden="true"></svg>
    <div class="board-drag-layer"></div>
  </div>`;
}

const ARROW_COLORS = {
  green: '#15781b',
  blue: '#003088',
  red: '#882020',
};

function drawArrow(svg, from, to, color, flip) {
  const p1 = squarePoint(from, flip);
  const p2 = squarePoint(to, flip);
  if (!p1 || !p2) return;
  const dx = p2.x - p1.x;
  const dy = p2.y - p1.y;
  const len = Math.hypot(dx, dy);
  if (len < 0.2) return;
  const ux = dx / len;
  const uy = dy / len;
  const head = Math.min(0.42, len * 0.5);
  const half = 0.25;
  const tip = p2;
  const baseX = tip.x - ux * head;
  const baseY = tip.y - uy * head;

  const line = document.createElementNS(SVG_NS, 'line');
  line.setAttribute('x1', p1.x.toFixed(3));
  line.setAttribute('y1', p1.y.toFixed(3));
  line.setAttribute('x2', (baseX + ux * 0.1).toFixed(3));
  line.setAttribute('y2', (baseY + uy * 0.1).toFixed(3));
  line.setAttribute('stroke', color);
  line.setAttribute('stroke-width', '0.17');
  line.setAttribute('stroke-linecap', 'round');

  const polygon = document.createElementNS(SVG_NS, 'polygon');
  polygon.setAttribute('points', [
    `${tip.x.toFixed(3)},${tip.y.toFixed(3)}`,
    `${(baseX - uy * half).toFixed(3)},${(baseY + ux * half).toFixed(3)}`,
    `${(baseX + uy * half).toFixed(3)},${(baseY - ux * half).toFixed(3)}`,
  ].join(' '));
  polygon.setAttribute('fill', color);

  const group = document.createElementNS(SVG_NS, 'g');
  group.setAttribute('opacity', '0.78');
  group.appendChild(line);
  group.appendChild(polygon);
  svg.appendChild(group);
}

function squareFromPoint(x, y, root) {
  const el = document.elementFromPoint(x, y);
  const sq = el && el.closest ? el.closest('.sq[data-square]') : null;
  return sq && root.contains(sq) ? sq.dataset.square : null;
}

/**
 * Wire pointer interaction to a rendered board.
 * opts.moves    — [{from, to, promotion, ...}] legal moves in algebraic notation
 * opts.onMove   — (move, {dragged}) => void, called on click-click or drop
 * opts.canMove  — () => boolean, re-checked on every interaction
 */
export function bindBoard(root, opts = {}) {
  if (!root) return;
  if (root.__boardApi) {
    // Доска уже отрисована и слушатели навешаны — обновляем доступные ходы
    // (они приходят асинхронно, например из /legal).
    root.__boardApi.update(opts);
    return;
  }

  const flip = root.dataset.flip === '1';
  const svg = root.querySelector('.board-arrows');
  const layer = root.querySelector('.board-drag-layer');
  const squares = new Map();
  root.querySelectorAll('.sq[data-square]').forEach(sq => squares.set(sq.dataset.square, sq));

  let moves = (opts.moves || []).filter(m => m && m.from && m.to);
  let canMove = typeof opts.canMove === 'function' ? opts.canMove : () => true;
  let onMove = typeof opts.onMove === 'function' ? opts.onMove : null;

  const arrows = [];
  let selected = null;
  let drag = null;
  let arrowDraft = null;
  let hoverSquare = null;
  let movedDistance = 0;

  const allowed = () => moves.length > 0 && canMove();

  function legalTargets(from) {
    const targets = new Map();
    for (const m of moves) {
      if (m.from !== from) continue;
      targets.set(m.to, (targets.get(m.to) || []).concat(m));
    }
    return targets;
  }

  function clearSelection() {
    selected = null;
    squares.forEach(sq => sq.classList.remove('sel', 'dot', 'cap'));
  }

  function select(from) {
    const targets = legalTargets(from);
    clearSelection();
    if (!targets.size) return false;
    selected = from;
    const origin = squares.get(from);
    if (origin) origin.classList.add('sel');
    targets.forEach((_, to) => {
      const sq = squares.get(to);
      if (!sq) return;
      sq.classList.add(sq.querySelector('.piece') ? 'cap' : 'dot');
    });
    return true;
  }

  function findMove(from, to) {
    const candidates = moves.filter(m => m.from === from && m.to === to);
    if (!candidates.length) return null;
    return candidates.find(m => (m.promotion || 'q') === 'q') || candidates[0];
  }

  function renderArrows() {
    svg.textContent = '';
    arrows.forEach(a => drawArrow(svg, a.from, a.to, a.color, flip));
    if (arrowDraft && arrowDraft.to && arrowDraft.to !== arrowDraft.from) {
      drawArrow(svg, arrowDraft.from, arrowDraft.to, arrowDraft.color, flip);
    }
  }

  function setHover(square) {
    if (hoverSquare === square) return;
    if (hoverSquare) {
      const prev = squares.get(hoverSquare);
      if (prev) prev.classList.remove('over');
    }
    hoverSquare = square;
    if (square) {
      const next = squares.get(square);
      if (next) next.classList.add('over');
    }
  }

  function startDrag(from, event) {
    const sq = squares.get(from);
    const piece = sq && sq.querySelector('.piece');
    if (!piece) return;
    const boardRect = root.getBoundingClientRect();
    const squareRect = sq.getBoundingClientRect();
    const size = boardRect.width / 8;
    const grabX = event.clientX - squareRect.left;
    const grabY = event.clientY - squareRect.top;

    const ghost = piece.cloneNode(true);
    ghost.classList.add('piece-ghost');
    ghost.style.width = size + 'px';
    ghost.style.height = size + 'px';
    ghost.style.transform = `translate(${squareRect.left - boardRect.left}px, ${squareRect.top - boardRect.top}px)`;
    layer.appendChild(ghost);
    piece.classList.add('dragging');

    drag = {
      from,
      ghost,
      piece,
      grabX,
      grabY,
      boardRect,
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
    };
    movedDistance = 0;
  }

  function moveDrag(event) {
    if (!drag) return;
    const x = event.clientX - drag.boardRect.left - drag.grabX;
    const y = event.clientY - drag.boardRect.top - drag.grabY;
    drag.ghost.style.transform = `translate(${x}px, ${y}px)`;
    movedDistance = Math.max(movedDistance, Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY));
    if (movedDistance > 3) drag.moved = true;
  }

  function endDrag(event) {
    if (!drag) return null;
    const current = drag;
    drag = null;
    const target = squareFromPoint(event.clientX, event.clientY, root) || current.from;
    current.ghost.remove();
    current.piece.classList.remove('dragging');
    setHover(null);

    if (!current.moved || target === current.from) return { from: current.from, target, dragged: false };
    return { from: current.from, target, dragged: true };
  }

  function finishMove(move, dragged) {
    clearSelection();
    if (onMove) onMove(move, { dragged });
  }

  root.addEventListener('contextmenu', e => e.preventDefault());

  root.addEventListener('pointerdown', event => {
    if (event.button === 2) {
      if (!svg) return;
      event.preventDefault();
      const from = squareFromPoint(event.clientX, event.clientY, root);
      if (!from) return;
      arrowDraft = {
        from,
        to: null,
        color: event.altKey ? ARROW_COLORS.red : event.shiftKey ? ARROW_COLORS.blue : ARROW_COLORS.green,
      };
      renderArrows();
      try { root.setPointerCapture(event.pointerId); } catch {}
      return;
    }
    if (event.button !== 0) return;

    const square = squareFromPoint(event.clientX, event.clientY, root);
    if (!square) return;
    event.preventDefault();

    if (arrows.length) {
      arrows.length = 0;
      renderArrows();
    }
    squares.forEach(sq => sq.classList.remove('rh'));

    if (!allowed()) { clearSelection(); return; }

    if (selected && findMove(selected, square)) {
      finishMove(findMove(selected, square), false);
      return;
    }

    if (moves.some(m => m.from === square)) {
      if (selected === square) { clearSelection(); return; }
      select(square);
      startDrag(square, event);
      try { root.setPointerCapture(event.pointerId); } catch {}
      return;
    }

    clearSelection();
  });

  root.addEventListener('pointermove', event => {
    if (arrowDraft) {
      const square = squareFromPoint(event.clientX, event.clientY, root);
      if (square) arrowDraft.to = square;
      renderArrows();
      return;
    }
    if (!drag) return;
    moveDrag(event);
    const square = squareFromPoint(event.clientX, event.clientY, root);
    setHover(square && square !== drag.from ? square : null);
  });

  root.addEventListener('pointerup', event => {
    if (arrowDraft) {
      const draft = arrowDraft;
      arrowDraft = null;
      const square = squareFromPoint(event.clientX, event.clientY, root);
      if (square && square !== draft.from) {
        arrows.push({ from: draft.from, to: square, color: draft.color });
      } else if (square) {
        const el = squares.get(square);
        if (el) el.classList.toggle('rh');
      }
      renderArrows();
      return;
    }
    if (!drag) return;
    const ended = endDrag(event);
    if (!ended) { clearSelection(); return; }
    const move = findMove(ended.from, ended.target);
    if (!move) { if (ended.dragged) clearSelection(); return; }
    finishMove(move, ended.dragged);
  });

  root.addEventListener('pointercancel', () => {
    if (drag) {
      drag.ghost.remove();
      drag.piece.classList.remove('dragging');
      drag = null;
    }
    arrowDraft = null;
    setHover(null);
    renderArrows();
  });

  root.__boardApi = {
    update(next = {}) {
      if (Array.isArray(next.moves)) {
        moves = next.moves.filter(m => m && m.from && m.to);
        clearSelection();
      }
      if (typeof next.canMove === 'function') canMove = next.canMove;
      if (typeof next.onMove === 'function') onMove = next.onMove;
      if (next.animate) playBoardAnimation(root);
    },
    clear() {
      arrows.length = 0;
      renderArrows();
      clearSelection();
      squares.forEach(sq => sq.classList.remove('rh'));
    },
  };

  if (opts.animate !== false) playBoardAnimation(root);
}

/** Slide the piece that just landed on the anim-to square from anim-from. */
export function playBoardAnimation(root) {
  if (!root) return;
  const from = root.dataset.animFrom;
  const to = root.dataset.animTo;
  if (!from || !to) return;
  const target = squaresFor(root).get(to);
  const piece = target && target.querySelector('.piece');
  if (!piece) return;

  const flip = root.dataset.flip === '1';
  const p1 = squarePoint(from, flip);
  const p2 = squarePoint(to, flip);
  const size = root.getBoundingClientRect().width / 8;
  if (!p1 || !p2 || !size) return;
  const dx = (p1.x - p2.x) * size;
  const dy = (p1.y - p2.y) * size;

  piece.style.transition = 'none';
  piece.style.transform = `translate(${dx}px, ${dy}px)`;
  piece.style.willChange = 'transform';
  void piece.offsetWidth;
  requestAnimationFrame(() => {
    piece.style.transition = 'transform 170ms cubic-bezier(0.22, 0.61, 0.36, 1)';
    piece.style.transform = 'translate(0, 0)';
  });
  piece.addEventListener('transitionend', () => {
    piece.style.transition = '';
    piece.style.transform = '';
    piece.style.willChange = '';
  }, { once: true });
}

function squaresFor(root) {
  const map = new Map();
  root.querySelectorAll('.sq[data-square]').forEach(sq => map.set(sq.dataset.square, sq));
  return map;
}
