/* One-click Clinical Co-Pilot launch on the patient dashboard.
 *
 * OpenEMR already renders a Launch button for every enabled SMART app, but it lives in the "SMART Enabled Apps" card,
 * which loads collapsed and sits below the Vitals card — three actions away when a physician has ~90 seconds. This
 * adds a button beside the patient name in the page heading.
 *
 * It does not reimplement the launch: it clicks OpenEMR's own button, so the CSRF token, client id, intent and the
 * full-screen dialog all stay in library/js/utility.js (oeSMART.initLaunch). If the app is not registered or the FHIR
 * API is off, that button does not exist and this adds nothing.
 *
 * Loaded only on the dashboard, through custom/assets/custom.yaml (loadInFile).
 */
(function () {
    'use strict';

    var BUTTON_ID = 'copilot-quick-launch';

    function smartButton() {
        var named = document.querySelector('.smart-launch-btn[data-smart-name*="Co-Pilot"]');
        return named || document.querySelector('.smart-launch-btn');
    }

    function addLaunchButton() {
        if (document.getElementById(BUTTON_ID)) {
            return;
        }
        var smart = smartButton();
        var brand = document.querySelector('nav.navbar .navbar-brand');
        if (!smart || !brand) {
            return;   // no enabled SMART app, or not a page with a heading: add nothing rather than a dead control
        }

        var button = document.createElement('button');
        button.id = BUTTON_ID;
        button.type = 'button';
        button.className = 'btn btn-primary btn-sm copilot-quick-launch';
        button.title = smart.dataset.smartName
            ? 'Open ' + smart.dataset.smartName + ' for this patient'
            : 'Open the Clinical Co-Pilot for this patient';

        var icon = document.createElement('i');
        icon.className = 'fa fa-notes-medical mr-1';
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(icon);
        button.appendChild(document.createTextNode(smart.dataset.smartName || 'Clinical Co-Pilot'));

        button.addEventListener('click', function () {
            smartButton().click();   // re-query: the card can re-render between page load and the click
        });

        brand.insertAdjacentElement('afterend', button);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addLaunchButton);
    } else {
        addLaunchButton();
    }
})();
