// Copy an invite link to the clipboard. Loaded as a file because the CSP blocks inline scripts.
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var input = document.getElementById(button.dataset.copy);
  if (!input) return;
  input.select();
  var done = function () {
    var label = button.textContent;
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = label; }, 2000);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(input.value).then(done, function () { input.select(); });
  } else {
    input.select();
  }
});
