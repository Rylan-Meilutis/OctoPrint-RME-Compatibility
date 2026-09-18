// NODE_PATH=<temporary jsdom/jquery/knockout dependencies> node tests/test_cancel_object_ui.js
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_objects.jinja2', 'utf8');
    const dom = new JSDOM('<ul><li id="overview-link"><a href="#tab_plugin_rme_compatibility_objects">Overview</a></li></ul>' +
        '<div id="tab_plugin_rme_compatibility_objects">' + template + '</div>' +
        '<div id="tab_plugin_cancelobject"><div><div id="cancel-table" data-bind="text: nativeLabel"></div></div></div>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko;
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    let refreshes = 0, clicked = null;
    vm.refreshObjects = () => refreshes++;
    vm.cancelOverviewObject = obj => { clicked = obj.id; };
    vm.cancelObjects([{id: 2, object: 'Benchy', cancelled: false}]);
    vm.objectPreview({bed: [[0, 0], [250, 0], [250, 220], [0, 220]], objects: [{name: 'Benchy', segments: [[5, 5, 10, 10]]}]});
    const native = {nativeLabel: ko.observable('Native list')};
    ko.applyBindings(native, w.document.getElementById('tab_plugin_cancelobject'));
    ko.applyBindings(vm, w.document.getElementById('tab_plugin_rme_compatibility_objects'));
    vm.integrateCancelObjectPreview();
    vm.integrateCancelObjectPreview(); // Idempotent, no extra bindings or lists.
    const preview = w.document.getElementById('rme-object-overview');
    assert.equal(preview.nextElementSibling.id, 'cancel-table');
    assert.equal(w.document.querySelectorAll('#rme-object-overview').length, 1);
    assert.equal(w.document.getElementById('overview-link').style.display, 'none');
    assert.equal(preview.querySelector('.rme-object-list').style.display, 'none');
    preview.querySelector('button').click();
    preview.querySelector('.rme-object-hit').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
    assert.equal(refreshes, 1);
    assert.equal(clicked, 2);
    native.nativeLabel('Still bound');
    assert.equal(w.document.getElementById('cancel-table').textContent, 'Still bound');
    vm.objectStatus('Updated preview');
    assert(preview.textContent.includes('Updated preview'));
    dom.window.close();
    console.log('Cancel Object embedded preview bindings and native list passed');
})();
