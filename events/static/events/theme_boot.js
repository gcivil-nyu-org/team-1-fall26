// Runs in <head> before first paint (a file, not inline, so the CSP can forbid inline scripts).
// Theme: the saved choice, else the OS setting. shell.js keeps it in sync after load.
// data-js marks that scripts run, so controls that need them (e.g. "Show all time zones") can show.
(function () {
    var saved = null;
    try { saved = localStorage.getItem("syncmily-theme"); } catch (e) {}
    var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = saved || (dark ? "dark" : "light");
    document.documentElement.dataset.js = "";
})();
