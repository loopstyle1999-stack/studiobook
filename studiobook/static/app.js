// Small enhancements. Every page keeps working without JavaScript, except the pop-up forms in the admin area.
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) { event.preventDefault(); }
});

document.querySelectorAll("[data-toggle]").forEach(function (box) {
  var target = document.getElementById(box.getAttribute("data-toggle"));
  function sync() { if (target) { target.hidden = !box.checked; } }
  box.addEventListener("change", sync); sync();
});

// Pop-up forms: <button data-open="id"> opens <dialog id="id">; clicking the backdrop or [data-close] closes it.
document.addEventListener("click", function (event) {
  var opener = event.target.closest("[data-open]");
  if (opener) {
    var dialog = document.getElementById(opener.getAttribute("data-open"));
    if (dialog && dialog.showModal) { dialog.showModal(); }
    return;
  }
  if (event.target.closest("[data-close]")) { var d = event.target.closest("dialog"); if (d) { d.close(); } return; }
  if (event.target.tagName === "DIALOG") { event.target.close(); }
});

// Class form: picking a class type fills in its usual length and capacity.
document.querySelectorAll("select[data-type-select]").forEach(function (select) {
  select.addEventListener("change", function () {
    var option = select.options[select.selectedIndex];
    var form = select.form;
    if (!option.value) { return; }
    form.elements.duration.value = option.getAttribute("data-duration") || form.elements.duration.value;
    form.elements.capacity.value = option.getAttribute("data-capacity") || form.elements.capacity.value;
    if (!form.elements.title.value) { form.elements.title.placeholder = option.text; }
  });
});

// Week calendar: click an empty spot to start a new class at that time.
document.querySelectorAll(".tg-col").forEach(function (column) {
  column.addEventListener("click", function (event) {
    if (event.target !== column) { return; }
    var dialog = document.getElementById("dlg-class");
    if (!dialog) { return; }
    var minutes = Math.round(event.offsetY / 15) * 15 + parseInt(column.getAttribute("data-lo"), 10) * 60;
    var hh = ("0" + Math.floor(minutes / 60)).slice(-2), mm = ("0" + (minutes % 60)).slice(-2);
    var field = dialog.querySelector("input[name=starts_at]");
    if (field) { field.value = column.getAttribute("data-date") + "T" + hh + ":" + mm; }
    dialog.showModal();
  });
});

// Installable app: lets phones offer "Add to Home screen" for the member pages.
if ("serviceWorker" in navigator && /^\/(me|offline)/.test(location.pathname)) {
  navigator.serviceWorker.register("/sw.js").catch(function () {});
}

// ---------------------------------------------------------------- 日期条横向滚动
// 滚轮、拖动、触控板都能用；打开时把选中的那天滚到中间
(function () {
  var strip = document.getElementById("strip");
  if (!strip) { return; }

  // 滚轮：竖向滚动转成横向
  strip.addEventListener("wheel", function (e) {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      strip.scrollLeft += e.deltaY;
      e.preventDefault();
    }
  }, { passive: false });

  // 鼠标拖动
  var down = false, startX = 0, startLeft = 0, moved = false;
  strip.addEventListener("mousedown", function (e) {
    down = true; moved = false; startX = e.pageX; startLeft = strip.scrollLeft;
    strip.classList.add("dragging");
  });
  window.addEventListener("mouseup", function () {
    down = false; strip.classList.remove("dragging");
  });
  strip.addEventListener("mousemove", function (e) {
    if (!down) { return; }
    var dx = e.pageX - startX;
    if (Math.abs(dx) > 4) { moved = true; }
    strip.scrollLeft = startLeft - dx;
    if (moved) { e.preventDefault(); }
  });
  // 拖动结束时不要触发链接点击
  strip.addEventListener("click", function (e) {
    if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; }
  }, true);

  // 打开时让选中的那天出现在视野中间
  var on = strip.querySelector(".day.on");
  if (on) {
    var target = on.offsetLeft - strip.clientWidth / 2 + on.offsetWidth / 2;
    strip.scrollTo({ left: Math.max(0, target), behavior: "instant" in strip ? "instant" : "auto" });
  }
})();

// ---------------------------------------------------------------- 月历收起状态
// 记住上次是展开还是收起，下次打开还是那样
(function () {
  var box = document.getElementById("cal");
  if (!box) { return; }
  try {
    if (localStorage.getItem("calOpen") === "0") { box.removeAttribute("open"); }
    box.addEventListener("toggle", function () {
      localStorage.setItem("calOpen", box.open ? "1" : "0");
    });
  } catch (e) { /* 隐私模式下 localStorage 不可用，忽略 */ }
})();
