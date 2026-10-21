const plannerDialog = document.getElementById("planner-dialog");
const dialogContent = document.getElementById("dialog-content");
const dialogTitle = document.getElementById("dialog-title");
const dialogNotice = document.getElementById("dialog-notice");
const dialogClose = document.getElementById("dialog-close");
const dialogPageLink = document.getElementById("dialog-page-link");
let dialogUrl = "";
let dialogEntry = "";
let returnFocus = null;
let dialogBusy = false;
let dialogSaving = false;
let savedChanges = false;
let activeRequest = null;
let pageRequest = null;
let eventLayout = "list";

function initialiseEventForm(root) {
    const form = root.querySelector("[data-event-form]");
    if (!form) return;
    form.dataset.wizard = "true";
    updateFamilyCount(form);
    const review = form.querySelector("[data-event-review]");
    const participantErrors = form.querySelector(".field-participants .errorlist");
    setEventStep(form, participantErrors ? "family" : review && !form.querySelector(".error-summary") ? "review" : "details");
}

function updateFamilyCount(form) {
    const count = form.querySelector("[data-family-count]");
    if (!count) return;
    const selected = form.querySelectorAll(".field-participants input:checked").length;
    count.textContent = `${selected} selected`;
}

function setEventLayout(layout) {
    eventLayout = layout;
    const collection = document.querySelector("[data-event-collection]");
    if (collection) collection.dataset.layout = layout;
    document.querySelectorAll("[data-event-layout]").forEach(button => {
        button.setAttribute("aria-pressed", String(button.dataset.eventLayout === layout));
    });
}

function setEventStep(form, step) {
    form.dataset.step = step;
    form.querySelectorAll("[data-event-step], [data-step-indicator]").forEach(button => {
        if ((button.dataset.eventStep || button.dataset.stepIndicator) === step) button.setAttribute("aria-current", "step");
        else button.removeAttribute("aria-current");
    });
    const review = form.querySelector("[data-event-review]");
    if (review) review.hidden = step !== "review";
}

function validEventDetails(form) {
    for (const input of form.querySelectorAll(".event-fields input, .event-fields select")) {
        if (input.type !== "checkbox" && !input.checkValidity()) {
            setEventStep(form, "details");
            input.reportValidity();
            return false;
        }
    }
    return true;
}

function setDialogBusy(busy, saving = false) {
    dialogBusy = busy;
    dialogSaving = busy && saving;
    plannerDialog.setAttribute("aria-busy", String(busy));
    plannerDialog.classList.toggle("is-loading", busy);
    dialogClose.disabled = dialogSaving;
    dialogContent.querySelectorAll("button[type='submit']").forEach(button => button.disabled = busy);
}

function showDialogError(message) {
    dialogNotice.textContent = message;
    dialogNotice.hidden = false;
}

function renderDialog(html, url) {
    const page = new DOMParser().parseFromString(html, "text/html");
    const main = page.querySelector("#main-content");
    if (!main) throw new Error("This page could not be opened. Try the full page instead.");
    main.querySelectorAll("script").forEach(script => script.remove());
    const ids = new Map();
    main.querySelectorAll("[id]").forEach(element => {
        ids.set(element.id, `dialog-${element.id}`);
        element.id = ids.get(element.id);
    });
    main.querySelectorAll("*").forEach(element => {
        for (const attribute of ["for", "aria-labelledby", "aria-describedby", "aria-controls", "aria-errormessage"]) {
            if (!element.hasAttribute(attribute)) continue;
            element.setAttribute(attribute, element.getAttribute(attribute).split(" ").map(id => ids.get(id) || id).join(" "));
        }
        const href = element.getAttribute("href");
        if (href?.startsWith("#") && ids.has(href.slice(1))) element.setAttribute("href", `#${ids.get(href.slice(1))}`);
    });
    dialogContent.replaceChildren(...Array.from(main.childNodes).map(node => document.importNode(node, true)));
    dialogUrl = url;
    dialogPageLink.href = url;
    const heading = dialogContent.querySelector("h1, h2");
    dialogTitle.textContent = plannerDialog.dataset.titleOverride || (heading ? heading.textContent.trim() : "Family planner");
    const workspace = dialogContent.querySelector(".event-workspace-context");
    plannerDialog.querySelector(".dialog-eyebrow").textContent = workspace ? `SYNCMILY / ${workspace.textContent.trim()}` : "SYNCMILY / FAMILY PLANNER";
    plannerDialog.dataset.screen = dialogContent.querySelector("[data-event-form]") ? "create" : dialogContent.querySelector(".event-detail") ? "event" : "workspace";
    initialiseEventForm(dialogContent);
    dialogContent.scrollTop = 0;
    const error = dialogContent.querySelector(".error-summary, .errorlist");
    const input = Array.from(dialogContent.querySelectorAll("input:not([type='hidden']), select")).find(element => element.getClientRects().length > 0);
    const target = error || input || dialogTitle;
    if (!target.matches("input, select, button, a")) target.tabIndex = -1;
    target.focus({preventScroll: true});
}

async function loadDialog(url, options = {}) {
    if (dialogBusy) return;
    dialogNotice.hidden = true;
    plannerDialog.querySelector(".dialog-loading span:last-child").textContent = options.body?.get("action") === "preview" ? "Checking your event…" : options.method === "POST" ? "Saving your changes…" : "Opening your planner…";
    setDialogBusy(true, options.method === "POST");
    const controller = new AbortController();
    activeRequest = controller;
    try {
        const response = await fetch(url, {...options, credentials: "same-origin", signal: controller.signal});
        if (!response.ok) throw new Error(response.status === 403 || response.status === 404 ? "You no longer have access to this page. Close this window to return to your schedule." : "Something went wrong. Your details are still here. Please try again.");
        if (new URL(response.url).pathname === "/accounts/login/") {
            window.location.assign(response.url);
            return;
        }
        const html = await response.text();
        if (!plannerDialog.open || activeRequest !== controller) return;
        if (options.method === "POST" && response.redirected) savedChanges = true;
        renderDialog(html, response.url);
    } catch (error) {
        if (error.name !== "AbortError") showDialogError(error instanceof TypeError ? "Couldn’t connect. Your details are still here. Please try again." : error.message);
    } finally {
        if (activeRequest === controller) setDialogBusy(false);
    }
}

function openPlannerDialog(link) {
    if (!plannerDialog.showModal) {
        window.location.assign(link.href);
        return;
    }
    returnFocus = link;
    if (dialogEntry === link.href && dialogContent.querySelector("[data-event-form]") && !savedChanges) {
        plannerDialog.showModal();
        return;
    }
    dialogEntry = link.href;
    plannerDialog.dataset.titleOverride = link.dataset.dialogTitle || "";
    savedChanges = false;
    dialogTitle.textContent = link.dataset.dialogTitle || link.textContent.trim();
    dialogPageLink.href = link.href;
    dialogContent.replaceChildren();
    plannerDialog.showModal();
    loadDialog(link.href);
}

async function loadPlannerPage(url, updateHistory = true) {
    if (pageRequest) pageRequest.abort();
    const controller = new AbortController();
    pageRequest = controller;
    const main = document.getElementById("main-content");
    main.classList.add("is-updating");
    try {
        const response = await fetch(url, {credentials: "same-origin", signal: controller.signal});
        if (!response.ok || new URL(response.url).pathname === "/accounts/login/") {
            window.location.assign(response.url);
            return;
        }
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const content = page.querySelector("#main-content");
        if (pageRequest !== controller) return;
        if (!content) throw new Error("Page unavailable");
        const swap = () => main.replaceChildren(...Array.from(content.childNodes).map(node => document.importNode(node, true)));
        // Let the browser slide the selected tab and cross-fade the schedule where it can.
        if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
            await document.startViewTransition(swap).updateCallbackDone;
        } else {
            swap();
        }
        document.title = page.title;
        if (updateHistory) history.pushState({planner: true}, "", response.url);
        initialiseEventForm(main);
        setEventLayout(eventLayout);
        window.applyLiquidGlass?.(main);
        const status = document.getElementById("planner-status");
        if (status) status.textContent = "Schedule updated.";
    } catch (error) {
        if (error.name !== "AbortError") window.location.assign(url);
    } finally {
        if (pageRequest === controller) main.classList.remove("is-updating");
    }
}

document.addEventListener("click", event => {
    const layoutButton = event.target.closest("[data-event-layout]");
    if (layoutButton) {
        setEventLayout(layoutButton.dataset.eventLayout);
        return;
    }
    const familyButton = event.target.closest("[data-family-select]");
    if (familyButton) {
        const form = familyButton.closest("form");
        form.querySelectorAll(".field-participants input[type='checkbox']").forEach(input => {
            input.checked = familyButton.dataset.familySelect === "all";
        });
        familyButton.dispatchEvent(new Event("input", {bubbles: true}));
        return;
    }
    const stepButton = event.target.closest("[data-event-step]");
    if (stepButton) {
        const form = stepButton.closest("form");
        if (stepButton.dataset.eventStep === "details" || validEventDetails(form)) setEventStep(form, stepButton.dataset.eventStep);
        return;
    }
    const link = event.target.closest("a[href]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.hasAttribute("download") || link.target) return;
    if (new URL(link.href).origin !== location.origin) return;
    if (plannerDialog.open && dialogContent.contains(link) && link.hash) {
        event.preventDefault();
        const target = dialogContent.querySelector(link.hash);
        if (target) {
            const form = target.closest("[data-event-form]");
            if (form) setEventStep(form, target.closest(".field-participants") ? "family" : "details");
            target.scrollIntoView({block: "center"});
            target.focus();
        }
        return;
    }
    if (link.hasAttribute("data-dialog") && !plannerDialog.open) {
        event.preventDefault();
        openPlannerDialog(link);
    } else if (plannerDialog.open && dialogContent.contains(link) && (link.hasAttribute("data-dialog") || link.classList.contains("editor-reset"))) {
        event.preventDefault();
        loadDialog(link.href);
    } else if (link.hasAttribute("data-planner-page")) {
        event.preventDefault();
        loadPlannerPage(link.href);
    }
});

document.addEventListener("submit", event => {
    const form = event.target;
    const inDialog = plannerDialog.open && dialogContent.contains(form);
    if (!inDialog && !form.hasAttribute("data-planner-form")) return;
    event.preventDefault();
    if (inDialog && dialogBusy) return;
    const data = new FormData(form);
    if (event.submitter?.name) data.append(event.submitter.name, event.submitter.value);
    const method = form.method.toUpperCase();
    const url = new URL(form.getAttribute("action") || (inDialog ? dialogUrl : location.href), location.origin);
    if (method === "GET") url.search = new URLSearchParams(data).toString();
    if (inDialog) loadDialog(url.href, method === "POST" ? {method, body: data} : {});
    else loadPlannerPage(url.href);
});

document.addEventListener("keydown", event => {
    const form = event.target.closest("[data-event-form]");
    if (form && form.dataset.step === "details" && event.key === "Enter" && event.target.matches("input")) {
        event.preventDefault();
        if (validEventDetails(form)) setEventStep(form, "family");
    }
});

document.addEventListener("input", event => {
    const form = event.target.closest("[data-event-form]");
    if (form) {
        updateFamilyCount(form);
        const review = form.querySelector("[data-event-review]");
        const notice = form.querySelector("[data-review-notice]");
        if (review) {
            review.hidden = true;
            notice.textContent = "Details changed. Check them again before saving.";
        }
    }
    if (event.target.matches("[data-event-search]")) {
        const query = event.target.value.trim().toLocaleLowerCase();
        const cards = document.querySelectorAll("[data-event-card]");
        let visible = 0;
        cards.forEach(card => {
            const matches = card.textContent.toLocaleLowerCase().includes(query);
            card.hidden = !matches;
            if (matches) visible++;
        });
        const empty = document.getElementById("search-empty");
        if (empty) empty.hidden = visible > 0 || !query;
    }
});

dialogClose.addEventListener("click", () => plannerDialog.close());
plannerDialog.addEventListener("cancel", event => {
    if (dialogSaving) event.preventDefault();
});
plannerDialog.addEventListener("click", event => {
    if (event.target !== plannerDialog || dialogSaving) return;
    const box = plannerDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) plannerDialog.close();
});
plannerDialog.addEventListener("close", () => {
    if (activeRequest && !dialogSaving) activeRequest.abort();
    activeRequest = null;
    setDialogBusy(false);
    if (savedChanges) window.location.reload();
    else if (returnFocus?.isConnected) returnFocus.focus();
});
window.addEventListener("popstate", () => loadPlannerPage(location.href, false));
initialiseEventForm(document);
