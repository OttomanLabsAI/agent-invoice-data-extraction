(function () {
  "use strict";

  // ---- Reveal / hide masked inputs
  document.querySelectorAll(".secret .reveal").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var wrap = btn.closest(".secret");
      var input = wrap.querySelector("input");
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      wrap.classList.toggle("shown", show);
      btn.setAttribute("aria-label", show ? "Hide" : "Show");
      btn.title = show ? "Hide" : "Show";
    });
  });

  // ---- Connection tests (post the current field values, no save needed)
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }
  function showResult(el, res) {
    el.textContent = res.message;
    el.className = "test-result " + (res.ok ? "ok" : "error");
  }

  var testClaude = document.getElementById("test-claude");
  if (testClaude) {
    testClaude.addEventListener("click", function () {
      var out = document.getElementById("test-claude-result");
      out.className = "test-result"; out.textContent = "Checking…";
      postJSON("/settings/test/claude", { key: document.getElementById("anthropic_api_key").value })
        .then(function (res) { showResult(out, res); })
        .catch(function () { showResult(out, { ok: false, message: "Request failed." }); });
    });
  }

  var testSage = document.getElementById("test-sage");
  if (testSage) {
    testSage.addEventListener("click", function () {
      var out = document.getElementById("test-sage-result");
      out.className = "test-result"; out.textContent = "Contacting Intacct…";
      postJSON("/settings/test/sage", {
        endpoint: document.getElementById("sage_endpoint").value,
        sender_id: document.getElementById("sage_sender_id").value,
        sender_password: document.getElementById("sage_sender_password").value,
        company_id: document.getElementById("sage_company_id").value,
        user_id: document.getElementById("sage_user_id").value,
        user_password: document.getElementById("sage_user_password").value
      }).then(function (res) { showResult(out, res); })
        .catch(function () { showResult(out, { ok: false, message: "Request failed." }); });
    });
  }

  // ---- Line-item editor
  var linesTable = document.getElementById("lines");
  if (linesTable) {
    var tbody = linesTable.querySelector("tbody");
    var template = document.getElementById("line-template");

    function num(v) { var n = parseFloat(String(v).replace(/,/g, "")); return isNaN(n) ? 0 : n; }
    function recalc() {
      var net = 0, vat = 0;
      tbody.querySelectorAll("tr").forEach(function (tr) {
        var netEl = tr.querySelector('[name="line_net"]');
        var rateEl = tr.querySelector('[name="line_vat_rate"]');
        var vatEl = tr.querySelector('[name="line_vat"]');
        var qtyEl = tr.querySelector('[name="line_qty"]');
        var priceEl = tr.querySelector('[name="line_unit_price"]');
        if (qtyEl.value !== "" && priceEl.value !== "" && netEl.dataset.auto !== "off") {
          netEl.value = (num(qtyEl.value) * num(priceEl.value)).toFixed(2);
        }
        if (rateEl.value !== "" && vatEl.dataset.auto !== "off") {
          vatEl.value = (num(netEl.value) * num(rateEl.value) / 100).toFixed(2);
        }
        net += num(netEl.value);
        vat += num(vatEl.value);
      });
      var netTotal = document.getElementById("net_total");
      var vatTotal = document.getElementById("vat_total");
      var grossTotal = document.getElementById("gross_total");
      if (netTotal && netTotal.dataset.auto !== "off") netTotal.value = net.toFixed(2);
      if (vatTotal && vatTotal.dataset.auto !== "off") vatTotal.value = vat.toFixed(2);
      if (grossTotal && grossTotal.dataset.auto !== "off") grossTotal.value = (num(netTotal.value) + num(vatTotal.value)).toFixed(2);
    }

    function wireRow(tr) {
      tr.querySelectorAll("input, select").forEach(function (el) {
        el.addEventListener("input", function () {
          // Typing directly into a computed field switches it to manual.
          if (el.name === "line_net" || el.name === "line_vat") el.dataset.auto = "off";
          recalc();
        });
      });
      tr.querySelector("button.rm").addEventListener("click", function () {
        tr.remove(); recalc();
      });
    }
    tbody.querySelectorAll("tr").forEach(wireRow);
    ["net_total", "vat_total", "gross_total"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener("input", function () { el.dataset.auto = "off"; if (id !== "gross_total") recalc(); });
    });
    var addBtn = document.getElementById("add-line");
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        var tr = template.content.firstElementChild.cloneNode(true);
        tbody.appendChild(tr);
        wireRow(tr);
        tr.querySelector("input").focus();
      });
    }
  }

  // ---- Payload tabs
  document.querySelectorAll(".tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var group = btn.closest(".tabs").parentElement;
      group.querySelectorAll(".tabs button").forEach(function (b) { b.classList.toggle("active", b === btn); });
      group.querySelectorAll(".tabpane").forEach(function (p) { p.classList.toggle("active", p.id === btn.dataset.tab); });
    });
  });

  // ---- Copy buttons
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var src = document.getElementById(btn.dataset.copy);
      if (!src) return;
      navigator.clipboard.writeText(src.textContent).then(function () {
        var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = old; }, 1200);
      });
    });
  });
})();

// ---- Keep a run going while the agents report more mail waiting
(function () {
  "use strict";
  var STOP_KEY = "invoice-agent-stop-run";
  document.querySelectorAll("[data-run]").forEach(function (btn) {
    btn.addEventListener("click", function () { try { sessionStorage.removeItem(STOP_KEY); } catch (e) {} });
  });
  var form = document.querySelector("form[data-continue]");
  if (!form) return;
  var nudge = document.getElementById("continue-nudge");
  var stopBtn = form.querySelector("[data-stop]");
  var countdown = nudge ? nudge.querySelector("[data-countdown]") : null;
  var stopped = false;
  try { stopped = sessionStorage.getItem(STOP_KEY) === "1"; } catch (e) {}
  if (stopped) {
    if (countdown) countdown.parentNode.textContent = "Stopped. Click Continue now to carry on.";
    return;
  }
  var left = 3;
  var timer = setInterval(function () {
    left -= 1;
    if (countdown) countdown.textContent = String(left);
    if (left <= 0) { clearInterval(timer); form.submit(); }
  }, 1000);
  if (stopBtn) {
    stopBtn.addEventListener("click", function () {
      clearInterval(timer);
      try { sessionStorage.setItem(STOP_KEY, "1"); } catch (e) {}
      if (countdown) countdown.parentNode.textContent = "Stopped. Click Continue now to carry on.";
    });
  }
})();
