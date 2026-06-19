// Integrations entry: a real <button> (so it matches the Weixin/Play button
// styling exactly) that opens the full knowledge console in a new tab.
(() => {
  function init() {
    const btn = document.getElementById("knowledge-console");
    if (!btn) return;
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.open("static/knowledge.html", "_blank", "noopener");
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
