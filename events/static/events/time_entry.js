// Time boxes that follow the 12h/24h switch. A browser's own <input type="time"> follows
// the computer's locale, not the page, so each one is swapped for hour : minute boxes,
// plus AM/PM in 12-hour mode. The original input stays in the form, hidden, holding
// "HH:MM": the server and the other scripts (end-time suggestion, photo scan) keep
// reading and writing it as before, and this keeps the visible boxes in step with it.
(function () {
    "use strict";

    var nativeValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");

    function twelveHour() {
        var mode = document.documentElement.dataset.clock;
        if (mode) return mode !== "24";
        return new Intl.DateTimeFormat(navigator.language, { hour: "numeric" }).resolvedOptions().hour12 !== false;
    }

    function pad(number) {
        return (number < 10 ? "0" : "") + number;
    }

    function parse(value) {
        var match = /^(\d{1,2}):(\d{2})/.exec(value || "");
        if (!match) return null;
        var hour = parseInt(match[1], 10), minute = parseInt(match[2], 10);
        return hour < 24 && minute < 60 ? { hour: hour, minute: minute } : null;
    }

    function enhance(input) {
        if (input.dataset.timeEntry) return;
        input.dataset.timeEntry = "yes";
        var label = (input.labels && input.labels[0] && input.labels[0].textContent.trim()) || "Time";

        var box = document.createElement("span");
        box.className = "time-entry";
        box.setAttribute("role", "group");
        box.setAttribute("aria-label", label);

        function field(name, placeholder) {
            var part = document.createElement("input");
            part.type = "text";
            part.inputMode = "numeric";
            part.maxLength = 2;
            part.autocomplete = "off";
            part.placeholder = placeholder;
            part.className = "time-entry-part";
            part.setAttribute("aria-label", name);
            return part;
        }
        var hour = field("Hour", "hh");
        var minute = field("Minutes", "mm");
        var colon = document.createElement("span");
        colon.className = "time-entry-colon";
        colon.textContent = ":";
        colon.setAttribute("aria-hidden", "true");
        var half = document.createElement("select");
        half.className = "time-entry-half";
        half.setAttribute("aria-label", "AM or PM");
        [["", "AM/PM"], ["AM", "AM"], ["PM", "PM"]].forEach(function (pair) {
            var option = document.createElement("option");
            option.value = pair[0];
            option.textContent = pair[1];
            half.appendChild(option);
        });
        box.append(hour, colon, minute, half);

        // The visible boxes take the label's focus and the required check.
        if (input.id) {
            hour.id = input.id + "-hour";
            Array.prototype.forEach.call(input.labels || [], function (labelEl) { labelEl.htmlFor = hour.id; });
        }
        if (input.required) hour.required = minute.required = true;
        if (input.getAttribute("aria-invalid") === "true") box.classList.add("is-invalid");
        input.type = "hidden";
        input.after(box);

        function render() {
            var time = parse(nativeValue.get.call(input));
            var twelve = twelveHour();
            half.hidden = !twelve;
            half.required = twelve && hour.required;
            hour.placeholder = twelve ? "h" : "hh";
            if (!time) {
                if (document.activeElement !== hour && document.activeElement !== minute) {
                    hour.value = minute.value = "";
                }
                return;
            }
            hour.value = twelve ? String(time.hour % 12 || 12) : pad(time.hour);
            minute.value = pad(time.minute);
            half.value = twelve ? (time.hour < 12 ? "AM" : "PM") : "";
        }

        // Typed boxes -> "HH:MM" in the real input (empty while incomplete or invalid).
        function store() {
            var twelve = twelveHour();
            var h = parseInt(hour.value, 10), m = parseInt(minute.value, 10);
            var value = "";
            var valid = !isNaN(h) && !isNaN(m) && m >= 0 && m <= 59 && minute.value.length > 0;
            if (valid && twelve) {
                valid = h >= 1 && h <= 12 && half.value !== "";
                if (valid) h = (h % 12) + (half.value === "PM" ? 12 : 0);
            } else if (valid) {
                valid = h >= 0 && h <= 23;
            }
            if (valid) value = pad(h) + ":" + pad(m);
            var typedSomething = hour.value || minute.value;
            box.classList.toggle("is-invalid", !valid && !!typedSomething && minute.value.length === 2);
            if (value === nativeValue.get.call(input)) return;
            nativeValue.set.call(input, value);
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        }

        // Scripts that set input.value (end-time suggestion, photo scan) update the boxes.
        Object.defineProperty(input, "value", {
            configurable: true,
            get: function () { return nativeValue.get.call(input); },
            set: function (value) { nativeValue.set.call(input, value); render(); },
        });

        hour.addEventListener("input", function () {
            hour.value = hour.value.replace(/\D/g, "");
            var limit = twelveHour() ? 1 : 2;
            // Jump to minutes once the hour can't take another digit (e.g. "9", "12", "23").
            if (hour.value.length === 2 || (hour.value.length === 1 && parseInt(hour.value, 10) > limit)) {
                minute.focus();
                minute.select();
            }
            store();
        });
        minute.addEventListener("input", function () {
            minute.value = minute.value.replace(/\D/g, "");
            if (minute.value.length === 2 && twelveHour() && !half.value) half.focus();
            store();
        });
        minute.addEventListener("blur", function () {
            if (minute.value.length === 1) minute.value = "0" + minute.value;
            store();
        });
        half.addEventListener("change", store);
        hour.addEventListener("blur", store);
        document.addEventListener("syncmily:clock", render);
        render();
    }

    function setup(root) {
        (root || document).querySelectorAll('input[type="time"]').forEach(enhance);
    }

    new MutationObserver(function () { setup(document); })
        .observe(document.documentElement, { childList: true, subtree: true });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { setup(); });
    else setup();
})();
