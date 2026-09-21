// Run with NODE_PATH pointing at temporary jsdom/jquery/knockout dependencies.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {runScripts: 'outside-only'});
    const w = dom.window;
    const $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.gettext = x => x;
    const notices = [];
    w.PNotify = function (notice) { notices.push(notice); };
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko;
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    vm.command = () => $.Deferred().resolve().promise();
    const linked = {material: 'PLA', profile: 'PLA-00D', firmware_profile: 'PLA-00D', color: '#808080'};
    assert.equal(vm.loadedFilamentLabel(linked), 'PLA');
    assert.equal(linked.profile, 'PLA-00D'); // Internal identity is untouched.
    assert.equal(vm.loadedFilamentLabel({profile: 'PLA', material: 'PLA'}), 'PLA');
    assert.equal(vm.loadedFilamentLabel({profile: 'PLA-00D', material: 'PLA-00D'}), 'Unassigned');
    assert(!vm.spoolLabel({alias: 'PLA-00D', material: 'PLA', display_name: 'PLA-00D'}).includes('PLA-00D'));
    const state = {supported: true, prompt: {kind: 'toolmap', requirements: [
        {logical: 0, material: 'PLA', color: '#ffffff'}], recommendation: {0: 1, 1: 0, 2: 2}},
        loaded_filaments: [{tool: 0, material: 'PLA', color: '#000000'},
            {tool: 1, material: 'PLA', color: '#ffffff'}, {tool: 2, material: 'PETG'}]};
    vm.state(state);
    vm.physicalTools([0, 1, 2]);
    vm.mappingRows([0, 1, 2].map(i => ({logical: i, physical: ko.observable(i)})));
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
    const controls = w.document.createElement('div');
    controls.innerHTML = template.slice(template.indexOf('<section id="rme-print-controls"'), template.indexOf('</section>') + 10);
    w.document.body.append(controls);
    ko.applyBindings(vm, controls);
    state.connected = true;
    state.machine = {tune: 1};
    state.tune = {lcd: 1, light: 1, screen_print: 80, chamber_print: 50, status_print: 20, updated: Date.now() / 1000};
    vm.state.valueHasMutated();
    const sent = [];
    vm.command = (command, data) => { sent.push({command, data}); return $.Deferred().resolve().promise(); };
    const lcd = controls.querySelector('input[aria-label="LCD Off or On"]');
    lcd.value = '0';
    lcd.dispatchEvent(new w.Event('change', {bubbles: true}));
    assert.equal(sent[0].data.kind, 'lcd');
    assert.equal(sent[0].data.value, 0);
    assert.equal(vm.tuneLight(), 1); // LCD never changes the chamber selector.
    assert.equal(lcd.value, '1'); // Wait for firmware acknowledgement.
    state.tune.lcd = 0;
    vm.state.valueHasMutated();
    assert.equal(lcd.value, '0');
    const start = template.indexOf('<div class="rme-routing-cards"');
    const end = template.indexOf('<label class="checkbox"><input type="checkbox" data-bind="checked: mappingEnabled">', start);
    const host = w.document.createElement('div');
    host.innerHTML = template.slice(start, end);
    w.document.body.append(host);
    ko.applyBindings(vm, host);
    assert.equal(host.querySelectorAll('.rme-tool-choice').length, 3);
    assert.equal(host.querySelectorAll('.rme-tool-choice:disabled').length, 1);
    host.querySelectorAll('.rme-tool-choice')[1].click();
    assert.equal(vm.mappingRows()[0].physical(), 1);
    assert.equal(vm.mappingRows()[1].physical(), 0);
    assert(vm.materialMappingValid());
    state.loaded_filaments[1].material = 'ASA';
    vm.state.valueHasMutated();
    assert(!vm.materialMappingValid());
    const modalCalls = [];
    $.fn.modal = function (action) { modalCalls.push([this.attr('id'), action]); return this; };
    $.fn.tab = function () { return this; };
    const mappingHost = w.document.createElement('div');
    mappingHost.id = 'rme-spool-mapping-host';
    w.document.body.append(mappingHost);
    const toolPanel = w.document.createElement('div');
    toolPanel.id = 'rme-tool-mapping';
    toolPanel.tabIndex = -1;
    w.document.body.append(toolPanel);
    // OctoPrint bootstrap-modal's layout() injects margin-top: -height / 2.
    // Our transform already centers the dialog, so the stylesheet must win
    // over that normal inline declaration (including after manager resize).
    const modalStyle = w.document.createElement('style');
    modalStyle.textContent = fs.readFileSync('octoprint_rme_compatibility/static/css/rme_compatibility.css', 'utf8');
    w.document.head.append(modalStyle);
    const centering = Array.from(modalStyle.sheet.cssRules).find(rule =>
        rule.selectorText && rule.selectorText.includes('#rme-tool-mapping.modal.fade.in')).style;
    assert.equal(parseFloat(centering.getPropertyValue('margin')), 0);
    assert.equal(centering.getPropertyPriority('margin'), 'important');
    assert.equal(centering.getPropertyValue('top'), '50%');
    assert.equal(centering.getPropertyValue('transform'), 'translate(-50%, -50%)');
    assert.equal(centering.getPropertyValue('max-height'), '90vh');
    assert.equal(centering.getPropertyValue('overflow-y'), 'auto');
    assert.equal(centering.getPropertyValue('box-sizing'), 'border-box');
    assert.equal(centering.getPropertyValue('transition'), 'opacity .3s linear');
    vm.updateToolmapNotice({kind: 'toolmap', updated: 1, filename: 'test.gcode', count: 1});
    assert.equal(notices.length, 0);
    assert.deepEqual(modalCalls.at(-1), ['rme-tool-mapping', 'show']);
    vm.settings.settings = {plugins: {rme_compatibility: {prompt_toolmap_on_print: ko.observable(false)}}};
    vm.updateToolmapNotice({kind: 'toolmap', updated: 2, filename: 'test.gcode', count: 1});
    assert.deepEqual(modalCalls.at(-1), ['rme-tool-mapping', 'hide']);
    assert.equal(modalCalls.filter(call => call[1] === 'show').length, 1);
    vm.showToolMapping(); // Explicit review is still available.
    assert.deepEqual(modalCalls.at(-1), ['rme-tool-mapping', 'show']);
    vm.spoolSelectionRows([{tool: 0, loaded: {material: 'PLA', color: '#ffffff'},
        selected: ko.observable(null), dirty: ko.observable(false),
        availableSpools: ko.observableArray([{database_id: 1, material: 'PLA', display_name: 'White PLA'}])}]);
    vm.showFilamentMapping();
    assert.equal(w.document.querySelector('#rme-spool-mapping-dialog'), null);
    assert(w.document.querySelector('#rme-spool-mapping-host #rme-spool-mapping-panel .rme-tool-choice'));
    w.document.querySelector('#rme-spool-mapping-panel .rme-tool-choice').click();
    assert.equal(vm.spoolSelectionRows()[0].selected(), 1);
    const save = w.document.querySelector('#rme-spool-mapping-panel .btn-primary');
    assert.equal(save.disabled, false);
    save.click();
    assert.equal(sent.at(-1).command, 'apply_spool_selections');
    assert.equal(sent.at(-1).data.selections[0].database_id, 1);
    assert.equal(vm.spoolSelectionRows()[0].dirty(), false);
    assert.equal(save.disabled, true);
    vm.showFilamentMapping();
    assert.equal(w.document.querySelectorAll('#rme-spool-mapping-panel').length, 1);
    const providerTab = w.document.createElement('div');
    providerTab.id = 'tab_spoolOverview';
    w.document.body.append(providerTab);
    vm.loginState.isUser = ko.observable(true);
    vm.onAllBound();
    const applyToPrinter = providerTab.querySelector('button[data-bind*="click: syncFilamentsToPrinter"]');
    assert(applyToPrinter);
    assert.equal(applyToPrinter.disabled, false);
    applyToPrinter.click();
    assert.equal(sent.at(-1).command, 'sync_filaments_to_printer');
    providerTab.querySelector('button[data-bind*="click: syncFilamentsFromPrinter"]').click();
    assert.equal(sent.at(-1).command, 'sync_filaments_from_printer');
    state.connected = false;
    vm.state.valueHasMutated();
    assert.equal(applyToPrinter.disabled, true);
    const dashboard = w.document.createElement('div');
    dashboard.id = 'tab_plugin_dashboard';
    dashboard.innerHTML = '<div><div class="dashboardProgressContainer"><span>Native print time</span></div></div>';
    const heaterGrid = w.document.createElement('div');
    heaterGrid.className = 'dashboardGridContainer';
    heaterGrid.innerHTML = '<div class="dashboardGridItem dashboard_threeQuarterGauge centreInGrid1" data-bind="css: gaugesCentreInGrid(\'fan\')"><svg width="140" height="140" viewBox="0 0 160 160"><path class="dashboardGauge" d="M30 120a60 60 0 1 1 90 0" stroke-dasharray="280"></path></svg></div>';
    dashboard.append(heaterGrid);
    const fan = heaterGrid.firstChild;
    w.document.body.append(dashboard);
    const nativeMarkup = dashboard.firstChild.firstChild.outerHTML;
    vm.settings.settings = {plugins: {rme_compatibility: {dashboard_rme_progress: ko.observable(true)}}};
    async function renderDashboard(workflow) {
        vm.state({supported: true, connected: true, workflow});
        vm.onAllBound();
        await new Promise(resolve => w.setTimeout(resolve, 30));
    }
    await renderDashboard(null);
    assert.equal(dashboard.querySelector('.rme-dashboard-logo text'), null);
    const logo = dashboard.querySelector('.rme-dashboard-logo');
    assert.equal(parseFloat(logo.getAttribute('x')) + parseFloat(logo.getAttribute('width')) / 2, 50);
    assert.equal(parseFloat(logo.getAttribute('y')) + parseFloat(logo.getAttribute('height')) / 2, 50);
    assert.equal(dashboard.querySelector('.rme-dashboard-percentage').textContent, '');
    assert.equal(dashboard.querySelector('.rme-dashboard-logo path').getAttribute('d'),
        'M4.5 3.5h4.1c2.05 0 3.4 1.18 3.4 3.02 0 1.31-.68 2.28-1.86 2.72l2.18 3.26H9.75L7.9 9.55H6.75v2.95H4.5zm2.25 1.85v2.42h1.6c.88 0 1.4-.44 1.4-1.22 0-.77-.52-1.2-1.4-1.2z');
    assert.equal(fan.nextElementSibling.id, 'rme-dashboard-progress');
    assert(fan.classList.contains('rme-dashboard-fan-neighbor'));
    assert.equal(dashboard.querySelector('.rme-dashboard-workflow-caption').textContent, 'Ready');
    await renderDashboard({workflow: 'tool_change', state: 'active', progress: 50});
    assert.equal(dashboard.querySelector('.rme-dashboard-percentage').textContent, '50%');
    const rmeArc = dashboard.querySelector('.rme-dashboard-workflow-gauge');
    assert.equal(rmeArc.getAttribute('d'), fan.querySelector('path').getAttribute('d'));
    assert.equal(rmeArc.getAttribute('stroke-dashoffset'), '140');
    assert.equal(rmeArc.parentElement.getAttribute('viewBox'), '0 0 160 160');
    assert.equal(dashboard.firstChild.firstChild.outerHTML, nativeMarkup);
    await renderDashboard(null);
    assert.equal(dashboard.querySelectorAll('#rme-dashboard-progress').length, 1);
    vm.settings.settings.plugins.rme_compatibility.dashboard_rme_progress(false);
    await renderDashboard(null);
    assert.equal(dashboard.querySelectorAll('#rme-dashboard-progress').length, 0);
    assert(!fan.classList.contains('rme-dashboard-fan-neighbor'));
    console.log('Graphical tool/spool mapping DOM bindings and interaction passed');
    dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
