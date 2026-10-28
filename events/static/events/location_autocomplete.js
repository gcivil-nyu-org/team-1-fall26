
"use strict";

(function () {
    function initializeLocationAutocomplete(form) {
        if (!form || form.dataset.locationAutocompleteInitialized === "true") {
            return;
        }

        const locationInput = form.querySelector('input[name="location"]');
        const latitudeInput = form.querySelector('input[name="latitude"]');
        const longitudeInput = form.querySelector('input[name="longitude"]');

        if (!locationInput || !latitudeInput || !longitudeInput) {
            return;
        }

        form.dataset.locationAutocompleteInitialized = "true";

        // Looks come from the page's stylesheet (paint.css), so the list follows light and dark.
        const wrapper = document.createElement("div");
        wrapper.className = "location-autocomplete";

        locationInput.parentNode.insertBefore(wrapper, locationInput);
        wrapper.appendChild(locationInput);

        const suggestions = document.createElement("div");
        suggestions.className = "location-suggestions";
        suggestions.setAttribute("role", "listbox");
        suggestions.setAttribute("aria-label", "Location suggestions");
        suggestions.hidden = true;
        suggestions.id = `${locationInput.id || "location"}-suggestions`;
        locationInput.setAttribute("aria-controls", suggestions.id);

        wrapper.appendChild(suggestions);

        const attribution = document.createElement("small");
        attribution.className = "location-attribution";
        attribution.textContent = "Place search by Photon and OpenStreetMap";
        wrapper.appendChild(attribution);

        let debounceTimer = null;
        let activeController = null;
        let requestVersion = 0;

        function clearCoordinates() {
            latitudeInput.value = "";
            longitudeInput.value = "";
        }

        function closeSuggestions() {
            suggestions.hidden = true;
            suggestions.replaceChildren();
            locationInput.setAttribute("aria-expanded", "false");
        }

        function showMessage(message) {
            suggestions.replaceChildren();

            const item = document.createElement("div");
            item.className = "location-message";
            item.textContent = message;

            suggestions.appendChild(item);
            suggestions.hidden = false;
            locationInput.setAttribute("aria-expanded", "true");
        }

        function formatLocation(properties) {
            const parts = [
                properties.name,
                [properties.housenumber, properties.street].filter(Boolean).join(" "),
                properties.city,
                properties.state,
                properties.country
            ].filter(Boolean);

            return [...new Set(parts)].join(", ");
        }

        function selectLocation(feature) {
            const coordinates = feature.geometry?.coordinates;

            if (
                !Array.isArray(coordinates) ||
                coordinates.length < 2 ||
                !Number.isFinite(coordinates[0]) ||
                !Number.isFinite(coordinates[1])
            ) {
                return;
            }

            locationInput.value = formatLocation(feature.properties || {});
            longitudeInput.value = String(coordinates[0]);
            latitudeInput.value = String(coordinates[1]);

            clearTimeout(debounceTimer);
            activeController?.abort();
            requestVersion += 1;

            closeSuggestions();
            locationInput.focus();
            locationInput.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function renderSuggestions(features) {
            suggestions.replaceChildren();

            if (!features.length) {
                showMessage("No matching locations found.");
                return;
            }

            features.forEach((feature) => {
                const coordinates = feature.geometry?.coordinates;
                const label = formatLocation(feature.properties || {});

                if (
                    !label ||
                    !Array.isArray(coordinates) ||
                    coordinates.length < 2
                ) {
                    return;
                }

                const option = document.createElement("button");
                option.type = "button";
                option.textContent = label;
                option.setAttribute("role", "option");

                option.addEventListener("click", () => {
                    selectLocation(feature);
                });

                suggestions.appendChild(option);
            });

            suggestions.hidden = suggestions.childElementCount === 0;
            locationInput.setAttribute(
                "aria-expanded",
                String(!suggestions.hidden)
            );
        }

        async function searchLocations(query) {
            activeController?.abort();

            const controller = new AbortController();
            activeController = controller;

            const currentVersion = ++requestVersion;

            const params = new URLSearchParams({
                q: query,
                limit: "5"
            });

            try {
                const response = await fetch(
                    `https://photon.komoot.io/api/?${params.toString()}`,
                    {
                        signal: controller.signal,
                        headers: {
                            Accept: "application/json"
                        }
                    }
                );

                if (!response.ok) {
                    throw new Error(`Photon returned ${response.status}`);
                }

                const data = await response.json();

                if (
                    currentVersion !== requestVersion ||
                    locationInput.value.trim() !== query
                ) {
                    return;
                }

                renderSuggestions(
                    Array.isArray(data.features) ? data.features : []
                );
            } catch (error) {
                if (error.name === "AbortError") {
                    return;
                }

                if (currentVersion === requestVersion) {
                    showMessage(
                        "Location search is temporarily unavailable. " +
                        "You can still enter an address manually."
                    );
                }

                console.error("Photon location search failed:", error);
            }
        }

        locationInput.setAttribute("role", "combobox");
        locationInput.setAttribute("aria-autocomplete", "list");
        locationInput.setAttribute("aria-expanded", "false");

        locationInput.addEventListener("input", () => {
            clearCoordinates();
            closeSuggestions();

            clearTimeout(debounceTimer);
            activeController?.abort();
            requestVersion += 1;

            const query = locationInput.value.trim();

            if (query.length < 3) {
                return;
            }

            debounceTimer = setTimeout(() => {
                searchLocations(query);
            }, 400);
        });

        locationInput.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeSuggestions();
            }
        });

        document.addEventListener("click", (event) => {
            if (!wrapper.contains(event.target)) {
                closeSuggestions();
            }
        });
    }

    function initializeAllLocationForms() {
        document.querySelectorAll("[data-event-form]").forEach((form) => {
            initializeLocationAutocomplete(form);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            initializeAllLocationForms
        );
    } else {
        initializeAllLocationForms();
    }

    const observer = new MutationObserver(() => {
        initializeAllLocationForms();
    });

    observer.observe(document.documentElement, {
        childList: true,
        subtree: true
    });
})();
