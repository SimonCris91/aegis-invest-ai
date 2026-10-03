import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('  function registerReadingAnchors()');
const end = source.indexOf('  function improveSections(');
assert.ok(start > 0 && end > start);

function harness() {
  const root = {
    details: [], rows: [],
    querySelectorAll(selector) {
      if (selector === 'details') return this.details;
      if (selector === '.pnl-history-row') return this.rows;
      if (selector === '[data-view-anchor]') return this.rows;
      throw new Error(`Unexpected selector: ${selector}`);
    },
  };
  const window = {scrollX: 0, scrollY: 1200, innerHeight: 800, lastScroll: null,
    scrollTo(options) { this.lastScroll = options; this.scrollY = options.top; }};
  const context = vm.createContext({root, window, Map});
  vm.runInContext(source.slice(start, end) + '\nthis.capture=captureReadingView; this.restore=restoreReadingView;', context);
  const detail = (className, open, top = -100) => ({
    id: '', dataset: {}, className, open,
    closest() { return {dataset: {}, className: 'capital-section'}; },
    querySelector() { return null; },
    getBoundingClientRect() { return {top}; },
  });
  const row = (textContent, top) => ({className:'pnl-history-row', textContent, dataset:{},
    getBoundingClientRect() { return {top}; }});
  return {root, window, context, detail, row};
}

test('all disclosures survive refresh even before toggle event has fired', () => {
  const {root, window, context, detail} = harness();
  root.details = [detail('pnl-history', true), detail('pnl-history order-history', true), detail('section-disclosure', false)];
  const state = context.capture();
  root.details = [detail('pnl-history', false), detail('pnl-history order-history', false), detail('section-disclosure', true)];
  window.scrollY = 200; // Browser clamped scroll after old content was removed.
  context.restore(state);
  assert.deepEqual(root.details.map(node => node.open), [true, true, false]);
  assert.equal(window.lastScroll.top, 1200);
  assert.equal(window.lastScroll.behavior, 'instant');
});

test('same history row stays at the same viewport offset when a new trade is inserted', () => {
  const {root, window, context, detail, row} = harness();
  root.details = [detail('pnl-history', true)];
  root.rows = [row('closed trade A', 80)];
  const state = context.capture();
  root.details = [detail('pnl-history', false)];
  root.rows = [row('new closed trade B', 100), row('closed trade A', 300)];
  context.restore(state);
  assert.equal(window.lastScroll.top, 1420);
  assert.equal(root.details[0].open, true);
});

test('missing row falls back to captured position without scrolling to another panel', () => {
  const {root, window, context, row} = harness();
  root.rows = [row('old trade', 80)];
  const state = context.capture();
  root.rows = [];
  context.restore(state);
  assert.equal(window.lastScroll.top, 1200);
});

test('benchmark rerender uses exactly the same view-preserving render function', () => {
  assert.match(source, /function render\(snapshot\) \{\s*const readingView/);
  assert.match(source, /bindModePanel\(\);\s*restoreReadingView\(readingView\)/);
  assert.match(source, /function loadBenchmark\(\)[\s\S]*render\(currentSnapshot\)/);
});
