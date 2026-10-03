// Landing page: the beetle that watches your cursor, the featured-specimen slides,
// the stats count-up, the card spotlight and the fireflies.
// Everything here is an enhancement: with JavaScript off the page still shows the beetle and the first specimen.
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var INTERVAL = 8000;

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  // ---------------------------------------------------------------- the beetle
  // The sprite sheet is 4 frames across and 3 down, numbered left to right, top to bottom:
  //   0 facing us            1 turning right (towards us)   2 profile, facing right   3 turning left (away)
  //   4 from behind          5 turning right (away)         6 profile, facing left    7 turning left (towards us)
  //   8 from above           9 belly                        10 head-on from above     11 from above (short)
  var COLS = 4;
  // the frame to show for each compass direction of the cursor, as seen from the beetle (screen coordinates)
  var FACING = { E: 2, SE: 1, S: 0, SW: 7, W: 6, NW: 3, N: 4, NE: 5 };
  var SECTORS = ["E", "SE", "S", "SW", "W", "NW", "N", "NE"]; // every 45 degrees, clockwise from "right"
  var TURNTABLE = ["S", "SE", "E", "NE", "N", "NW", "W", "SW"]; // the order it turns through when left alone
  var ABOVE = 8, BELLY = 9;

  function initBeetle(hero) {
    var beetle = hero.querySelector(".lp-beetle");
    var tilt = hero.querySelector(".lp-beetle-tilt");
    var layers = [].slice.call(hero.querySelectorAll(".lp-beetle-sprite"));
    var hint = hero.querySelector(".lp-hint");
    if (!beetle || !tilt || layers.length < 2) return;

    var shown = 0;            // which of the two layers is on screen
    var frame = 0;
    var sector = 2;           // index into SECTORS; starts facing us ("S")
    var overBeetle = false;
    var pointer = null;       // last known pointer position
    var dirty = false;
    var lastMove = 0;
    var flipUntil = 0;
    var idleStep = 0;
    var idleTimer = null;
    var visible = true;

    function setFrame(n) {
      if (n === frame) return;
      frame = n;
      var next = layers[1 - shown];
      next.style.setProperty("--col", String(n % COLS));
      next.style.setProperty("--row", String(Math.floor(n / COLS)));
      next.classList.add("is-on");
      layers[shown].classList.remove("is-on");
      shown = 1 - shown;
    }

    function angleDiff(a, b) {
      var d = Math.abs(a - b) % 360;
      return d > 180 ? 360 - d : d;
    }

    function look() {
      dirty = false;
      if (!pointer || performance.now() < flipUntil) return;
      var box = tilt.getBoundingClientRect();
      var cx = box.left + box.width / 2, cy = box.top + box.height / 2;
      var dx = pointer.x - cx, dy = pointer.y - cy;
      var dist = Math.hypot(dx, dy);
      var size = box.width;

      // the beetle leans a little towards the cursor
      var nx = Math.max(-1, Math.min(1, dx / (window.innerWidth * 0.5)));
      var ny = Math.max(-1, Math.min(1, dy / (window.innerHeight * 0.5)));
      tilt.style.transform = "translate3d(" + (nx * 12).toFixed(1) + "px," + (ny * 9).toFixed(1) + "px,0) rotate(" + (nx * 3).toFixed(2) + "deg)";

      // with the cursor right on top of it, it looks up at it (seen from above)
      var onTop = dist < size * (overBeetle ? 0.3 : 0.24);
      overBeetle = onTop;
      if (onTop) { setFrame(ABOVE); return; }

      // otherwise it turns to face the cursor; stick with the current direction until the cursor is clearly past the edge
      var deg = Math.atan2(dy, dx) * 180 / Math.PI;
      if (deg < 0) deg += 360;
      if (angleDiff(deg, sector * 45) > 22.5 + 7) sector = Math.round(deg / 45) % 8;
      setFrame(FACING[SECTORS[sector]]);
    }

    function request() {
      if (!dirty) { dirty = true; requestAnimationFrame(look); }
    }

    function stopIdle() {
      if (idleTimer) { clearInterval(idleTimer); idleTimer = null; }
    }

    // left alone, it slowly turns on the spot so the page is never frozen
    function startIdle() {
      if (idleTimer || reduceMotion || !visible || document.hidden) return;
      idleTimer = setInterval(function () {
        if (performance.now() < flipUntil) return;
        tilt.style.transform = "";
        idleStep = (idleStep + 1) % TURNTABLE.length;
        sector = SECTORS.indexOf(TURNTABLE[idleStep]);
        setFrame(FACING[TURNTABLE[idleStep]]);
        // after a full turn, stop on the front view for a while before starting round again
      }, 1300);
    }

    function onMove(e) {
      if (reduceMotion) return;
      pointer = { x: e.clientX, y: e.clientY };
      lastMove = performance.now();
      stopIdle();
      if (hint) hint.classList.add("is-gone");
      request();
    }

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onMove, { passive: true });

    // no movement for a few seconds (or a touch screen): back to turning on the spot
    setInterval(function () {
      if (reduceMotion) return;
      if (performance.now() - lastMove > 3500) {
        pointer = null;
        startIdle();
      }
    }, 800);

    // click or tap the beetle: it flips over and shows its belly
    beetle.addEventListener("pointerdown", function () {
      if (reduceMotion) return;
      flipUntil = performance.now() + 1500;
      setFrame(BELLY);
      beetle.classList.remove("is-flipped");
      void beetle.offsetWidth;
      beetle.classList.add("is-flipped");
      setTimeout(function () { dirty = true; look(); }, 1550);
    });

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
        if (!visible) stopIdle();
      }).observe(hero);
    }
    document.addEventListener("visibilitychange", function () { if (document.hidden) stopIdle(); });
    window.addEventListener("resize", request);

    if (!reduceMotion) startIdle();
  }

  // ---------------------------------------------------------------- hero slides (backgrounds and specimen panels)
  function initSlides(hero) {
    var bgs = [].slice.call(hero.querySelectorAll(".lp-bg[data-bg]"));
    var infos = [].slice.call(hero.querySelectorAll(".lp-info"));
    var total = infos.length;
    var current = 0;
    var timer = null;
    var paused = false;

    var countEl = hero.querySelector("[data-lp-current]");
    var progress = hero.querySelector(".lp-progress");
    var stack = hero.querySelector(".lp-info-stack");

    function markLoaded(img) {
      if (img.complete && img.naturalWidth) img.classList.add("is-loaded");
      else img.addEventListener("load", function () { img.classList.add("is-loaded"); }, { once: true });
    }

    function load(i) {
      if (i < 0 || i >= total) return;
      var bg = bgs[i];
      if (bg && bg.dataset.bg && !bg.dataset.loaded) {
        bg.style.backgroundImage = "url('" + bg.dataset.bg + "')";
        bg.dataset.loaded = "1";
      }
      var img = infos[i].querySelector(".lp-thumb img");
      if (img) {
        if (img.dataset.src) { img.src = img.dataset.src; img.removeAttribute("data-src"); }
        markLoaded(img);
      }
    }

    function show(i, fromUser) {
      current = (i + total) % total;
      load(current);
      load((current + 1) % total); // warm the next one
      bgs.forEach(function (el, k) { el.classList.toggle("is-active", k === current); });
      infos.forEach(function (el, k) {
        el.classList.toggle("is-active", k === current);
        el.setAttribute("aria-hidden", k === current ? "false" : "true");
        [].forEach.call(el.querySelectorAll("a"), function (a) { a.tabIndex = k === current ? 0 : -1; });
      });
      if (countEl) countEl.textContent = String(current + 1).padStart(2, "0");
      if (fromUser && stack) stack.setAttribute("aria-live", "polite"); // announce changes only once the user is driving
      restart();
    }

    function restart() {
      clearTimeout(timer);
      if (progress) {
        progress.classList.remove("is-running");
        void progress.offsetWidth; // restart the CSS animation
      }
      if (total < 2 || reduceMotion || paused) return;
      if (progress) progress.classList.add("is-running");
      timer = setTimeout(function () { show(current + 1, false); }, INTERVAL);
    }

    function pause() { paused = true; clearTimeout(timer); if (progress) progress.classList.remove("is-running"); }
    function resume() { paused = false; restart(); }

    var prev = hero.querySelector("[data-lp-prev]");
    var next = hero.querySelector("[data-lp-next]");
    if (prev) prev.addEventListener("click", function () { show(current - 1, true); });
    if (next) next.addEventListener("click", function () { show(current + 1, true); });

    // hovering the specimen panel or the controls pauses the slides (the rest of the hero is the beetle's playground)
    [].forEach.call(hero.querySelectorAll(".lp-side, .lp-controls"), function (el) {
      el.addEventListener("mouseenter", pause);
      el.addEventListener("mouseleave", resume);
    });
    hero.addEventListener("focusin", pause);
    hero.addEventListener("focusout", resume);
    document.addEventListener("visibilitychange", function () { document.hidden ? pause() : resume(); });

    hero.addEventListener("keydown", function (e) {
      if (total < 2) return;
      if (e.key === "ArrowRight") { show(current + 1, true); e.preventDefault(); }
      else if (e.key === "ArrowLeft") { show(current - 1, true); e.preventDefault(); }
    });

    // swipe on touch screens
    var startX = null;
    hero.addEventListener("pointerdown", function (e) { if (e.pointerType === "touch") startX = e.clientX; });
    hero.addEventListener("pointerup", function (e) {
      if (startX === null) return;
      var dx = e.clientX - startX;
      startX = null;
      if (Math.abs(dx) > 60) show(current + (dx < 0 ? 1 : -1), true);
    });

    if (total < 2) {
      [].forEach.call(hero.querySelectorAll("[data-lp-prev], [data-lp-next], .lp-progress"), function (el) { el.hidden = true; });
    }
    if (total) show(0, false);
  }

  // ---------------------------------------------------------------- fireflies
  function initMotes(hero) {
    var box = hero.querySelector(".lp-motes");
    if (!box || reduceMotion) return;
    for (var i = 0; i < 16; i++) {
      var m = document.createElement("i");
      m.style.setProperty("--x", (Math.random() * 100).toFixed(1) + "%");
      m.style.setProperty("--s", (2 + Math.random() * 3).toFixed(1) + "px");
      m.style.setProperty("--d", (11 + Math.random() * 12).toFixed(1) + "s");
      m.style.setProperty("--delay", (-Math.random() * 20).toFixed(1) + "s");
      m.style.setProperty("--sway", String(Math.round(-60 + Math.random() * 120)));
      box.appendChild(m);
    }
  }

  // ---------------------------------------------------------------- stats count-up
  function countUp(el) {
    var target = Number(el.dataset.count);
    if (!isFinite(target) || target <= 0 || reduceMotion || !window.digitGroups) return;
    var t0 = null, dur = 1400;
    function frame(t) {
      if (t0 === null) t0 = t;
      var p = Math.min((t - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.innerHTML = window.digitGroups(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(frame);
    }
    el.innerHTML = window.digitGroups(0);
    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- card spotlight
  function initCards(root) {
    [].forEach.call(root.querySelectorAll(".lp-card"), function (card) {
      card.addEventListener("pointermove", function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty("--mx", e.clientX - r.left + "px");
        card.style.setProperty("--my", e.clientY - r.top + "px");
      });
    });
  }

  ready(function () {
    var root = document.getElementById("lp-root");
    if (!root) return;
    var hero = root.querySelector(".lp-hero");
    if (hero) { initBeetle(hero); initSlides(hero); initMotes(hero); }
    [].forEach.call(root.querySelectorAll("[data-count]"), countUp);
    initCards(root);
  });
})();
