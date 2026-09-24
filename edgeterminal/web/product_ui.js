"use strict";
// Presentation only. Probabilities arrive from the sealed backend pairings.
const EdgeProduct = (() => {
  const groups = {
    all: null,
    passing: new Set(["passing_yards"]),
    rushing: new Set(["rushing_yards", "rushing_attempts"]),
    receiving: new Set(["receiving_yards", "receptions"]),
  };
  function filterSortProps(rows, filter = "all", sort = "confidence") {
    const allowed = groups[filter] || null;
    const visible = rows.filter(row => !allowed || allowed.has(row.market));
    const byPlayer = (a, b) => a.player.localeCompare(b.player) ||
      a.market.localeCompare(b.market) || a.sportsbook.localeCompare(b.sportsbook);
    if (sort === "player") visible.sort(byPlayer);
    else if (sort === "market") visible.sort((a, b) =>
      a.market.localeCompare(b.market) || byPlayer(a, b));
    else visible.sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1) || byPlayer(a, b));
    return visible;
  }
  return {filterSortProps};
})();
if (typeof module !== "undefined") module.exports = EdgeProduct;
