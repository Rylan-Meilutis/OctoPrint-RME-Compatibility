const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM(`<div id="tab_plugin_dashboard"><div class="dashboardGridContainer">
      <div id="fan" class="dashboard_threeQuarterGauge" data-bind="css: gaugesCentreInGrid('fan')">
      <svg style="width:140px;height:140px" viewBox="0 0 140 140"><path class="dashboardGauge" d="native-arc" stroke-dasharray="250" /></svg></div>
      </div></div><div id="state"><div class="progress"></div></div>`, {runScripts:'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = []; w.gettext = x => x; w.PNotify = function () {};
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko, settings = {plugins:{rme_compatibility:{dashboard_rme_progress:ko.observable(true)},
        dashboard:{useFahrenheit:ko.observable(false)}}};
    const native = {widgetsSettings:ko.observableArray([{title:'Native fan',enabled:ko.observable(true)}]), gaugesCentreInGrid:()=>''};
    ko.applyBindings(native, w.document.querySelector('#tab_plugin_dashboard'));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{settings}, {}, {}, {}]);
    vm.onAllBound(); vm.installDashboardWidgets();
    assert.equal(native.widgetsSettings().length, vm.dashboardWidgetOptions.length+1);
    const row = key => native.widgetsSettings().find(item=>item.rmeKey===key);
    const setting = key => settings.plugins.rme_compatibility[key];
    assert.equal(row('dashboard_dock_fan').enabled, setting('dashboard_dock_fan'));
    assert.equal(row('dashboard_dock_fan').enabled(), false, 'new optional sensors default off');
    assert.equal($('.rme-dashboard-sensor').length, 0);
    const snapshot = extra => ({supported:true, connected:true, tool_changes:{active:true,file:'a',remaining:0,total:0},
        workflow:{workflow:'probing',state:'active',progress:50}, tune:{updated:Date.now()/1000,
            dock_fan:128,dock_rpm:3000,heatbreak_pwm:255,heatbreak_rpm:6000,buddy_mcu:35.5,head_board:'unknown', ...extra}});
    vm.acceptState(snapshot());
    row('dashboard_dock_fan').enabled(true);
    row('dashboard_heatbreak_fan').enabled(true);
    row('dashboard_buddy_mcu').enabled(true);
    row('dashboard_head_board').enabled(true);
    assert.equal($('#rme-dashboard_dock_fan .rme-sensor-value').text(), '50%');
    assert.equal($('#rme-dashboard_dock_fan .rme-sensor-detail').text(), '3000 RPM');
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-value').text(), '100%');
    assert.equal($('#rme-dashboard_buddy_mcu .rme-sensor-value').text(), '35.5°C');
    assert.equal($('#rme-dashboard_head_board .rme-sensor-value').text(), '—');
    assert.equal($('#rme-dashboard_dock_fan path').first().attr('d'), 'native-arc');
    assert.equal($('#rme-dashboard_dock_fan > svg').css('height'), '140px');
    settings.plugins.dashboard.useFahrenheit(true);
    assert.equal($('#rme-dashboard_buddy_mcu .rme-sensor-value').text(), '95.9°F');
    await new Promise(resolve=>w.setTimeout(resolve,30));
    assert.equal($('#rme-workflow-overlay').length,1);
    row('dashboard_rme_bar').enabled(false);
    row('dashboard_rme_progress').enabled(false);
    row('dashboard_tool_changes').enabled(false);
    await new Promise(resolve=>w.setTimeout(resolve,30));
    assert.equal($('#rme-workflow-overlay').length,0);
    assert.equal($('#rme-dashboard-progress').length,0);
    assert.equal($('#rme-tool-changes-dashboard').length,0);
    assert.equal($('#state .progress').length,1,'native progress bar remains');
    assert.equal($('#fan').length,1,'native fan remains');
    row('dashboard_dock_fan').enabled(false);
    assert.equal($('#rme-dashboard_dock_fan').length,0);
    assert.equal(ko.toJS(settings).plugins.rme_compatibility.dashboard_rme_bar,false,'native Dashboard toggle uses persisted RME setting');
    vm.acceptState(snapshot({heatbreak_pwm:0,heatbreak_rpm:-1,buddy_mcu:-1}));
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-value').text(),'0%');
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-detail').text(),'RPM unavailable');
    assert.equal($('#rme-dashboard_buddy_mcu .rme-sensor-value').text(),'—','uninitialized sentinel is not a temperature');
    vm.tick(Date.now()+20000);
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-value').text(),'—','stale is not zero');
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-detail').text(),'Waiting for telemetry');
    vm.acceptState({connected:false,supported:true});
    assert.equal($('.rme-dashboard-sensor').length,0);
    vm.tick(Date.now()); vm.acceptState(snapshot({heatbreak_pwm:64}));
    assert.equal($('#rme-dashboard_heatbreak_fan .rme-sensor-value').text(),'25%');
    vm.installDashboardWidgets();
    assert.equal(native.widgetsSettings().length,vm.dashboardWidgetOptions.length+1,'reconnect does not duplicate settings');
    w.close();
    console.log('Native Dashboard widget toggles, persistence, telemetry units, unavailable/stale readings and reconnect passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
