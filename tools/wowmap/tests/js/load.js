// Loads pure functions out of the page's classic scripts into a vm context, so they can be
// tested without a browser. Whole files that touch the DOM or Leaflet at load (map.js) are
// not run: `fns` slices out named `function` declarations instead.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const STATIC = path.join(__dirname, '..', '..', 'wowmap', 'static');
const read = (file) => fs.readFileSync(path.join(STATIC, file), 'utf8');

// Source of `function name(...) {...}`, top-level or nested in an IIFE (indented): ends at the
// first line that is the same indent plus `}` (or on the same line for a one-liner), which is how
// the files are formatted. The indent is stripped so the body parses as a top-level declaration.
function fnSource(src, name) {
  const lines = src.split('\n');
  const start = lines.findIndex((l) => new RegExp(`^\\s*function ${name}\\(`).test(l));
  if (start < 0) throw new Error(`function ${name} not found`);
  const indent = lines[start].match(/^\s*/)[0];
  for (let i = start; i < lines.length; i++) {
    if (lines[i] === indent + '}' || (i === start && lines[i].trimEnd().endsWith('}'))) {
      return lines.slice(start, i + 1).map((l) => l.slice(indent.length)).join('\n');
    }
  }
  throw new Error(`function ${name} does not end`);
}

// `fns('map.js', ['pxOf'], 'const MAP_FRAME_W = 1002;')` -> context with those functions.
function fns(file, names, prelude = '', globals = {}) {
  const src = read(file);
  const ctx = vm.createContext({...globals});
  vm.runInContext(prelude + '\n' + names.map((n) => fnSource(src, n)).join('\n'), ctx);
  return ctx;
}

module.exports = {read, fns, fnSource, STATIC};
