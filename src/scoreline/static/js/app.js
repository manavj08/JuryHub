// Scoreline: small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  // Copy buttons: <button data-copy="#input-id">
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.querySelector(btn.getAttribute("data-copy"));
      if (!input) return;
      input.select();
      var done = function () {
        var label = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = label; }, 1600);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
      else { document.execCommand("copy"); done(); }
    });
  });

  // Confirm before destructive forms: <form data-confirm="Are you sure?">
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Filters start collapsed on small screens so results come first.
  if (window.matchMedia("(max-width: 900px)").matches) {
    document.querySelectorAll("details[data-collapse-mobile]").forEach(function (d) { d.open = false; });
  }

  // Submit on change: sort and filter selects.
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  // Deadline countdown next to an absolute time.
  document.querySelectorAll("[data-countdown]").forEach(function (el) {
    var end = new Date(el.getAttribute("data-countdown")).getTime();
    function tick() {
      var s = Math.floor((end - Date.now()) / 1000);
      if (s <= 0) { el.textContent = ", which has passed. Reload the page."; return; }
      var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      var parts = d ? [d + (d === 1 ? " day" : " days"), h + " h"] : h ? [h + " h", m + " min"] : [m + " min"];
      el.textContent = " (" + parts.join(" ") + " from now)";
      var dl = document.querySelector("[data-deadline]");
      if (dl && s < 3 * 3600) dl.classList.add("soon");
    }
    tick();
    setInterval(tick, 30000);
  });

  // Rubric editor: live percentages as weights change.
  document.querySelectorAll("form[data-weights]").forEach(function (form) {
    var inputs = form.querySelectorAll("[data-w]");
    function update() {
      var sum = 0;
      inputs.forEach(function (i) { sum += Math.max(0, parseFloat(i.value) || 0); });
      inputs.forEach(function (i) {
        var pct = i.closest(".weight-row").querySelector("[data-pct]");
        pct.textContent = sum ? Math.round(100 * Math.max(0, parseFloat(i.value) || 0) / sum) + "%" : "";
      });
    }
    inputs.forEach(function (i) { i.addEventListener("input", update); });
  });

  // The scoresheet: live weighted total and keyboard scoring.
  var sheet = document.querySelector("form[data-scoresheet]");
  if (!sheet) return;
  var crits = Array.prototype.slice.call(sheet.querySelectorAll("[data-crit]"));
  var totalEl = sheet.querySelector("[data-total]");
  var noteEl = sheet.querySelector("[data-total-note]");
  var active = 0;

  function total() {
    var acc = 0, wsum = 0, complete = true, scored = 0;
    crits.forEach(function (c) {
      var picked = c.querySelector("input:checked");
      var w = parseFloat(c.dataset.weight), lo = +c.dataset.min, hi = +c.dataset.max;
      if (!picked) { complete = false; return; }
      scored += 1;
      acc += w * (1 + 4 * (picked.value - lo) / (hi - lo));
      wsum += w;
    });
    if (!wsum) { totalEl.textContent = "\u2013"; totalEl.classList.add("pending"); noteEl.textContent = "out of 5"; return; }
    totalEl.textContent = (acc / wsum).toFixed(2);
    totalEl.classList.toggle("pending", !complete);
    noteEl.textContent = complete ? "out of 5" : "so far, " + scored + " of " + crits.length + " scored";
  }

  function setActive(i) {
    active = Math.max(0, Math.min(crits.length - 1, i));
    crits.forEach(function (c, j) { c.classList.toggle("active", j === active); });
  }

  crits.forEach(function (c, i) {
    c.addEventListener("focusin", function () { setActive(i); });
    c.addEventListener("click", function () { setActive(i); });
  });
  sheet.addEventListener("change", total);

  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (t && (t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type !== "radio") || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (sheet.querySelector("fieldset[disabled]")) return;
    var c = crits[active];
    if (/^[0-9]$/.test(e.key) && c) {
      var input = c.querySelector('input[value="' + e.key + '"]');
      if (input) {
        input.checked = true;
        total();
        e.preventDefault();
        if (active < crits.length - 1) setActive(active + 1);
      }
    } else if (e.key === "j" || e.key === "J") { setActive(active + 1); e.preventDefault(); }
    else if (e.key === "k" || e.key === "K") { setActive(active - 1); e.preventDefault(); }
  });

  var firstEmpty = crits.findIndex(function (c) { return !c.querySelector("input:checked"); });
  setActive(firstEmpty === -1 ? 0 : firstEmpty);
  total();
})();
