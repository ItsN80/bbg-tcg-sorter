// Light / dark theme, remembered per browser. Loaded in <head> without
// defer so the saved theme is on <html> before the page is drawn (no flash
// of the light theme). theme.css holds the colours.
(function () {
    var KEY = "bbg-theme";

    function saved() {
        try {
            return localStorage.getItem(KEY);
        } catch (e) {
            return null;  // storage blocked: fall back to light
        }
    }

    function apply(theme) {
        document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
    }

    apply(saved());

    window.bbgToggleTheme = function () {
        var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        apply(next);
        try {
            localStorage.setItem(KEY, next);
        } catch (e) {
            // not remembered, but this page still switches
        }
    };

    // Other open tabs of the sorter follow a switch.
    window.addEventListener("storage", function (e) {
        if (e.key === KEY) apply(e.newValue);
    });
})();
