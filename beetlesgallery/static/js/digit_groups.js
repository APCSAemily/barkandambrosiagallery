// Large numbers in groups of three ("70 000"), the same as the digit_groups template filter.
//   digitGroups(n, decimals)      -> HTML (one span per group), for innerHTML
//   digitGroupsText(n, decimals)  -> plain text with a narrow space between groups, for textContent or a title
(function () {
  function split(value, decimals) {
    var n = Number(value);
    if (value === null || value === "" || !isFinite(n)) return null;
    var text = Math.abs(n).toFixed(Math.max(decimals || 0, 0));
    var parts = text.split(".");
    var groups = [];
    for (var whole = parts[0]; whole; whole = whole.slice(0, -3)) groups.unshift(whole.slice(-3));
    return { sign: n < 0 && /[1-9]/.test(text) ? "-" : "", groups: groups, fraction: parts[1] ? "." + parts[1] : "" };
  }
  window.digitGroups = function (value, decimals) {
    var p = split(value, decimals);
    if (!p) return String(value);
    return p.sign + p.groups.map(function (g, i) {
      return '<span class="digit-group"' + (i ? ' style="margin-left:0.4em"' : "") + ">" + g + "</span>";
    }).join("") + p.fraction;
  };
  window.digitGroupsText = function (value, decimals) {
    var p = split(value, decimals);
    return p ? p.sign + p.groups.join(" ") + p.fraction : String(value);
  };
})();
