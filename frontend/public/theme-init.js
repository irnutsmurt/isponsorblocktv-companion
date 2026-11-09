// Applies the saved theme before first paint (CSP-safe external script).
try {
  var t = localStorage.getItem("isbtv-theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) { /* storage unavailable: follow system theme */ }
