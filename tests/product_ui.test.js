const test = require("node:test");
const assert = require("node:assert/strict");
const {filterSortProps} = require("../edgeterminal/web/product_ui.js");

const rows = [
  {player:"Zoe", market:"passing_yards", sportsbook:"A", confidence:0.76},
  {player:"Amy", market:"receptions", sportsbook:"A", confidence:0.83},
  {player:"Bea", market:"rushing_yards", sportsbook:"A", confidence:0.67},
  {player:"Cal", market:"receiving_yards", sportsbook:"A", confidence:0.70},
];

test("highest stored probability is default order without mutating input", () => {
  const out = filterSortProps(rows);
  assert.deepEqual(out.map(x => x.player), ["Amy","Zoe","Cal","Bea"]);
  assert.equal(rows[0].player, "Zoe");
});
test("filter and sort use only supported market groups", () => {
  assert.deepEqual(filterSortProps(rows,"receiving","player").map(x => x.player), ["Amy","Cal"]);
  assert.deepEqual(filterSortProps(rows,"rushing","market").map(x => x.player), ["Bea"]);
  assert.deepEqual(filterSortProps(rows,"passing","confidence").map(x => x.player), ["Zoe"]);
});
