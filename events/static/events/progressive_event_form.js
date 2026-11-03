"use strict";

(function () {
    function initializeProgressiveEventForm(form) {
        if (!form || form.dataset.progressiveInitialized === "true") {
            return;
        }

        form.dataset.progressiveInitialized = "true";
        form.dataset.progressiveReady = "true";

        const titleInput = form.querySelector('input[name="title"]');
        const dateInput = form.querySelector('input[name="date"]');
        const startTimeInput = form.querySelector('input[name="start_time"]');
        const endTimeInput = form.querySelector('input[name="end_time"]');
        const endDateInput = form.querySelector('input[name="end_date"]');
        const toggleEndDate = form.querySelector("[data-toggle-end-date]");
        const timesContainer = form.querySelector("[data-progressive-times]");
        const endDateField = form.querySelector("[data-end-date-field]");

        const locationInput = form.querySelector('input[name="location"]');
        const latitudeInput = form.querySelector('input[name="latitude"]');
        const longitudeInput = form.querySelector('input[name="longitude"]');
        const locationDetails = form.querySelector(
            "[data-progressive-location-details]"
        );
        const transportSelect = form.querySelector(
            'select[name="transport_mode"]'
        );
        const rideCheckbox = form.querySelector(
            'input[name="requires_transport"]'
        );
        const driversContainer = form.querySelector(
            "[data-progressive-drivers]"
        );
        const dropoffSelect = form.querySelector(
            'select[name="dropoff_driver"]'
        );
        const pickupSelect = form.querySelector(
            'select[name="pickup_driver"]'
        );

        const whoPick = form.querySelector(".who-pick");
        const whoToggle = form.querySelector("[data-who-toggle]");
        const whoDrawer = form.querySelector("[data-who-drawer]");
        const whoEmpty = form.querySelector("[data-who-empty]");
        const whoSelectedAvatars = form.querySelector(
            "[data-who-selected-avatars]"
        );
        const whoToggleText = form.querySelector(".who-toggle-text");

        const moreOptions = form.querySelector("[data-more-options]");

        // Step 2: Date and Time Progressive Logic
        function updateTimesVisibility() {
            if (!timesContainer) return;
            const hasDate = Boolean(dateInput && dateInput.value.trim());
            const hasTimeValues = Boolean(
                (startTimeInput && startTimeInput.value.trim()) ||
                (endTimeInput && endTimeInput.value.trim())
            );
            const hasTimeErrors = Boolean(
                form.querySelector(
                    ".field-start_time .errorlist, .field-end_time .errorlist"
                ) ||
                (startTimeInput &&
                    startTimeInput.getAttribute("aria-invalid") === "true") ||
                (endTimeInput &&
                    endTimeInput.getAttribute("aria-invalid") === "true")
            );

            if (hasDate || hasTimeValues || hasTimeErrors) {
                timesContainer.hidden = false;
            } else {
                timesContainer.hidden = true;
            }
        }

        function autoSuggestEndTime() {
            if (!startTimeInput || !endTimeInput) return;
            const startVal = startTimeInput.value.trim();
            if (!startVal) return;

            const parts = startVal.split(":");
            if (parts.length < 2) return;
            const h = parseInt(parts[0], 10);
            const m = parseInt(parts[1], 10);
            if (isNaN(h) || isNaN(m)) return;

            const nextH = (h + 1) % 24;
            const suggested =
                String(nextH).padStart(2, "0") + ":" + String(m).padStart(2, "0");

            if (
                !endTimeInput.value ||
                endTimeInput.dataset.autoSuggested === "true"
            ) {
                endTimeInput.value = suggested;
                endTimeInput.dataset.autoSuggested = "true";
                endTimeInput.dispatchEvent(new Event("input", { bubbles: true }));
            }
        }

        if (dateInput) {
            dateInput.addEventListener("input", updateTimesVisibility);
            dateInput.addEventListener("change", updateTimesVisibility);
        }

        if (startTimeInput) {
            startTimeInput.addEventListener("change", autoSuggestEndTime);
            startTimeInput.addEventListener("input", () => {
                if (startTimeInput.value.length === 5) {
                    autoSuggestEndTime();
                }
            });
        }

        if (endTimeInput) {
            endTimeInput.addEventListener("input", () => {
                delete endTimeInput.dataset.autoSuggested;
            });
        }

        function updateEndDateVisibility() {
            if (!toggleEndDate || !endDateField) return;
            if (toggleEndDate.checked) {
                endDateField.hidden = false;
            } else {
                endDateField.hidden = true;
                if (endDateInput) {
                    endDateInput.value = "";
                }
            }
        }

        if (toggleEndDate) {
            toggleEndDate.addEventListener("change", updateEndDateVisibility);
        }

        // Step 4: Location & Transportation Mode Progressive Logic
        function updateLocationDetailsVisibility() {
            if (!locationDetails) return;
            const locVal = locationInput ? locationInput.value.trim() : "";
            const hasLocation = locVal.length > 0;
            const hasLocErrors = Boolean(
                form.querySelector(
                    ".field-transport .errorlist, .field-ride-toggle .errorlist, " +
                    ".field-dropoff_driver .errorlist, .field-pickup_driver .errorlist"
                )
            );

            if (hasLocation || hasLocErrors) {
                locationDetails.hidden = false;
            } else {
                locationDetails.hidden = true;
                if (latitudeInput) latitudeInput.value = "";
                if (longitudeInput) longitudeInput.value = "";
                if (rideCheckbox && rideCheckbox.checked) {
                    rideCheckbox.checked = false;
                    updateDriversVisibility();
                }
            }
        }

        if (locationInput) {
            locationInput.addEventListener("input", updateLocationDetailsVisibility);
            locationInput.addEventListener("change", updateLocationDetailsVisibility);
        }

        // Step 5: Ride & Driver Assignment Progressive Logic
        function updateDriversVisibility() {
            if (!driversContainer) return;
            const isRideChecked = Boolean(rideCheckbox && rideCheckbox.checked);
            const hasDriverErrors = Boolean(
                form.querySelector(
                    ".field-dropoff_driver .errorlist, .field-pickup_driver .errorlist"
                )
            );

            if (isRideChecked || hasDriverErrors) {
                driversContainer.hidden = false;
            } else {
                driversContainer.hidden = true;
                if (dropoffSelect) dropoffSelect.value = "";
                if (pickupSelect) pickupSelect.value = "";
            }
        }

        if (rideCheckbox) {
            rideCheckbox.addEventListener("change", updateDriversVisibility);
        }

        // Step 3: Compact Family Members Selection Logic
        function updateWhoSummary() {
            if (!whoPick) return;
            const checkedBoxes = whoPick.querySelectorAll(
                'input[name="participants"]:checked'
            );

            if (checkedBoxes.length === 0) {
                if (whoEmpty) whoEmpty.hidden = false;
                if (whoSelectedAvatars) whoSelectedAvatars.replaceChildren();
                if (whoToggleText) whoToggleText.textContent = "Choose members";
            } else {
                if (whoEmpty) whoEmpty.hidden = true;
                if (whoToggleText) {
                    whoToggleText.textContent =
                        "Edit (" + checkedBoxes.length + " selected)";
                }
                if (whoSelectedAvatars) {
                    whoSelectedAvatars.replaceChildren();
                    checkedBoxes.forEach((checkbox) => {
                        const label = checkbox.closest("label");
                        if (!label) return;
                        const nameSpan = label.querySelector('[translate="no"]');
                        const name = nameSpan ? nameSpan.textContent.trim() : "";
                        const hue = label.dataset.hue || "";
                        const customColor = label.style.getPropertyValue("--hue") || "";

                        const chip = document.createElement("span");
                        chip.className = "pill who-avatar-chip";
                        chip.setAttribute("translate", "no");
                        if (hue) chip.dataset.hue = hue;
                        if (customColor) chip.style.setProperty("--hue", customColor);
                        chip.textContent = name;
                        whoSelectedAvatars.appendChild(chip);
                    });
                }
            }
        }

        if (whoToggle && whoDrawer) {
            whoToggle.addEventListener("click", (e) => {
                e.preventDefault();
                const willOpen = whoDrawer.hidden;
                whoDrawer.hidden = !willOpen;
                whoToggle.setAttribute("aria-expanded", String(willOpen));
            });
        }

        if (whoPick) {
            whoPick.addEventListener("change", (e) => {
                if (
                    e.target &&
                    e.target.matches('input[name="participants"]')
                ) {
                    updateWhoSummary();
                }
            });
        }

        // Step 6: More Options Auto-Expand on Error
        if (moreOptions) {
            const hasMoreOptionsErrors = Boolean(
                moreOptions.querySelector(".errorlist, [aria-invalid='true']")
            );
            const isTentativeChecked = Boolean(
                form.querySelector('input[name="is_tentative"]:checked')
            );
            if (hasMoreOptionsErrors || isTentativeChecked) {
                moreOptions.open = true;
            }
        }

        // Submission Safeguards
        form.addEventListener("submit", () => {
            // If ride is disabled, ensure stale drivers are not sent
            if (!rideCheckbox || !rideCheckbox.checked) {
                if (dropoffSelect) dropoffSelect.value = "";
                if (pickupSelect) pickupSelect.value = "";
            }
            // If location is blank, clear stale coordinates
            if (locationInput && !locationInput.value.trim()) {
                if (latitudeInput) latitudeInput.value = "";
                if (longitudeInput) longitudeInput.value = "";
            }
            // If date is set but times are missing, ensure times are visible
            if (dateInput && dateInput.value.trim() && timesContainer) {
                timesContainer.hidden = false;
            }
        });

        // Initialize state on load
        updateTimesVisibility();
        updateEndDateVisibility();
        updateLocationDetailsVisibility();
        updateDriversVisibility();
        updateWhoSummary();
    }

    function initializeAllForms() {
        document.querySelectorAll("[data-event-form]").forEach((form) => {
            initializeProgressiveEventForm(form);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializeAllForms);
    } else {
        initializeAllForms();
    }

    const observer = new MutationObserver(() => {
        initializeAllForms();
    });

    observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
    });
})();
