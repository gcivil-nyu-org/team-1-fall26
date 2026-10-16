const eventForm = document.getElementById("event-form");
const eventReview = document.getElementById("event-review");
const reviewNotice = document.getElementById("review-notice");

if (eventReview) {
    function clearEventReview() {
        eventReview.hidden = true;
        reviewNotice.textContent = "Details changed. Check your event details again to update the preview.";
    }

    eventForm.addEventListener("input", clearEventReview);
    eventForm.addEventListener("change", clearEventReview);
}
