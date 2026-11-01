document.addEventListener("DOMContentLoaded", function () {
  var toggle = document.getElementById("sidebarToggle");
  if (toggle) toggle.addEventListener("click", function () {
    document.querySelector(".app-sidebar").classList.toggle("show");
  });
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!window.confirm(el.getAttribute("data-confirm"))) { e.preventDefault(); e.stopPropagation(); }
    });
  });
  document.querySelectorAll("form[data-confirm-submit]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm-submit"))) e.preventDefault();
    });
  });
});
document.body && document.body.addEventListener("htmx:configRequest", function (evt) {
  var token = document.querySelector("meta[name=csrf-token]");
  if (token) evt.detail.headers["X-CSRFToken"] = token.content;
});
