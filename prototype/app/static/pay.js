// Payment forms on the customer page. Optional helper: the forms work without it.
// "Fill" puts the rest of the received amount on a bill (up to what that bill still owes),
// and a line under the bills says how the money is split and what is kept as credit.
document.querySelectorAll("[data-pay-form]").forEach(function (form) {
  var total = form.querySelector("[data-total]");
  var inputs = Array.prototype.slice.call(form.querySelectorAll("[data-left]"));
  var summary = form.querySelector("[data-summary]");
  function num(v) { return parseFloat(String(v || "").replace(/[,₹\s]/g, "")) || 0; }
  function fmt(n) { return n.toLocaleString("en-IN", { maximumFractionDigits: 2 }); }
  function sumExcept(skip) {
    return inputs.reduce(function (s, i) { return i === skip ? s : s + num(i.value); }, 0);
  }
  function update() {
    if (!summary) return;
    var split = sumExcept(null);
    var left = num(total.value) - split;
    if (!num(total.value) && !split) { summary.textContent = ""; return; }
    var isCredit = form.getAttribute("data-pay-form") === "credit";
    if (left < 0) {
      summary.textContent = "The bills add up to ₹" + fmt(-left) + " more than " + (isCredit ? "the credit." : "the amount.");
    } else {
      summary.textContent = isCredit
        ? "₹" + fmt(split) + " of credit used, ₹" + fmt(left) + " left as credit."
        : "₹" + fmt(split) + " on bills, ₹" + fmt(left) + " kept as credit.";
    }
  }
  form.querySelectorAll("[data-fill]").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-fill"));
      var billLeft = num(input.getAttribute("data-left"));
      var room = num(total.value) ? Math.max(num(total.value) - sumExcept(input), 0) : billLeft;
      input.value = Math.min(billLeft, room);
      update();
    });
  });
  [total].concat(inputs).forEach(function (i) { i.addEventListener("input", update); });
  update();
});
