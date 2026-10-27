// The light switch and the 12h/24h clock switch in the top bar.
// Times inside [data-clock] are written 24-hour by the server (`H:i`) and formatted here.
(function () {
    var root = document.documentElement;
    function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
    function save(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }

    document.addEventListener("DOMContentLoaded", function () {
        // Light switch: follows the OS until flipped, then remembers the choice.
        var themeSwitch = document.getElementById("theme-switch");
        var themeColor = document.querySelector('meta[name="theme-color"]');
        function syncTheme() {
            if (themeSwitch) themeSwitch.setAttribute("aria-checked", String(root.dataset.theme === "dark"));
            if (themeColor) themeColor.content = root.dataset.theme === "dark" && document.body.classList.contains("paint") ? "#1b1e1c" : "#ffffff";
        }
        syncTheme();
        if (themeSwitch) themeSwitch.addEventListener("click", function () {
            root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
            save("syncmily-theme", root.dataset.theme);
            syncTheme();
        });
        var os = window.matchMedia("(prefers-color-scheme: dark)");
        os.addEventListener("change", function (e) {
            if (load("syncmily-theme")) return;
            root.dataset.theme = e.matches ? "dark" : "light";
            syncTheme();
        });

        // Clock switch: 12-hour (US) or 24-hour, defaulting to the browser's locale.
        var clockSwitch = document.getElementById("clock-switch");
        var clocks = document.querySelectorAll("[data-clock]");
        var f12 = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
        var f12hour = new Intl.DateTimeFormat("en-US", { hour: "numeric", hour12: true });
        var f24 = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
        function setClock(mode, remember) {
            var use24 = mode === "24";
            clocks.forEach(function (el) {
                var parts = el.dataset.clock.split(":");
                var moment = new Date(2026, 0, 1, +parts[0], +parts[1]);
                el.textContent = use24 ? f24.format(moment) : ("axis" in el.dataset ? f12hour : f12).format(moment);
            });
            if (clockSwitch) clockSwitch.setAttribute("aria-checked", String(use24));
            root.dataset.clock = mode;
            if (remember) save("syncmily-clock", mode);
        }
        var localeHour12 = new Intl.DateTimeFormat(navigator.language, { hour: "numeric" }).resolvedOptions().hour12;
        setClock(load("syncmily-clock") || (localeHour12 === false ? "24" : "12"), false);
        if (clockSwitch) clockSwitch.addEventListener("click", function () {
            setClock(root.dataset.clock === "24" ? "12" : "24", true);
        });
    });
})();
