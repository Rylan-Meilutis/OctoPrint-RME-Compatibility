const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<html><body></body></html>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = []; w.gettext = value => value;
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    let pause = 0, toggle = 0, dialog;
    const controls = {isPaused: w.ko.observable(false),
        onlyPause() {++pause;}, pause() {++toggle;}};
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, controls, {}]);
    vm.state({supported: true});
    w.showConfirmationDialog = options => {dialog = options;};
    vm.installPauseConfirmation(); vm.installPauseConfirmation();
    controls.onlyPause(); assert.equal(pause, 0);
    assert.equal(dialog.cancel, 'No, continue the print');
    dialog.onproceed(); assert.equal(pause, 1);
    controls.pause(); assert.equal(toggle, 0);
    dialog.onproceed(); assert.equal(toggle, 1);
    // Cancel/no response does not call a pause API.
    controls.pause(); assert.equal(toggle, 1);
    controls.isPaused(true); dialog = null;
    controls.pause(); assert.equal(toggle, 2); assert.equal(dialog, null);
    // Non-RME printers retain their original behavior.
    controls.isPaused(false); vm.state({supported: false});
    controls.onlyPause(); assert.equal(pause, 2); assert.equal(dialog, null);
    // Fallback when OctoPrint's shared confirmation helper is unavailable.
    vm.state({supported: true}); delete w.showConfirmationDialog;
    w.confirm = () => false; controls.onlyPause(); assert.equal(pause, 2);
    w.confirm = () => true; controls.onlyPause(); assert.equal(pause, 3);
    dom.window.close();
    console.log('Manual pause confirmation, cancellation, resume and non-RME fallback passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
