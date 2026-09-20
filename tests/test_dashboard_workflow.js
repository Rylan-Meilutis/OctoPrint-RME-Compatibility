const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM(`<div id="tab_plugin_dashboard"><div class="dashboardGridContainer">
      <div id="fan" class="dashboardGridItem dashboard_threeQuarterGauge centreInGrid1"
        data-bind="css: gaugesCentreInGrid('fan')"><svg style="width:140px;height:140px" viewBox="0 0 140 140">
        <path class="dashboardGauge" d="native-arc" stroke-dasharray="250" /></svg></div>
      </div></div>`, {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const enabled = w.ko.observable(true);
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([
        {settings: {plugins: {rme_compatibility: {dashboard_rme_progress: enabled}}}}, {}, {}, {}
    ]);
    async function render(connected, supported) {
        vm.state({connected, supported});
        vm.onAllBound();
        await new Promise(resolve => w.setTimeout(resolve, 30));
    }
    await render(false, false);
    assert.equal($('#rme-dashboard-progress').length, 0);
    await render(true, false);
    assert.equal($('#rme-dashboard-progress').length, 0);
    await render(true, true);
    assert.equal($('#fan').next().attr('id'), 'rme-dashboard-progress');
    assert.equal($('#rme-dashboard-progress path.rme-dashboard-track').attr('d'), 'native-arc');
    assert.equal($('#rme-dashboard-progress > svg').css('height'), '140px');
    await render(true, true);
    assert.equal($('#rme-dashboard-progress').length, 1);
    await render(false, true); // Stale capabilities after disconnect must not keep it visible.
    assert.equal($('#rme-dashboard-progress').length, 0);
    assert.equal($('#fan').hasClass('rme-dashboard-fan-neighbor'), false);
    await render(true, true);
    await render(true, false); // Switching printers removes the old RME gauge.
    assert.equal($('#rme-dashboard-progress').length, 0);
    enabled(false);
    await render(true, true);
    assert.equal($('#rme-dashboard-progress').length, 0);
    const css = fs.readFileSync('octoprint_rme_compatibility/static/css/rme_compatibility.css', 'utf8');
    const rule = css.match(/#rme-dashboard-progress\s*\{([^}]+)\}/)[1];
    assert(rule.includes('display: block'));
    assert(!/padding\s*:|vertical-align\s*:|flex-direction\s*:/.test(rule));
    dom.window.close();
    console.log('Dashboard RME connection gating, reconnect, grid placement and native spacing passed');
})();
