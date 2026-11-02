// Real layout regression: node with playwright, jquery and knockout available.
// Optional RME_DASHBOARD_CSS exercises the complete upstream stylesheet too.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
(async function () {
    const browser = await chromium.launch({headless:true});
    try {
        const page = await browser.newPage();
        // Reproduce native grid, blanket font overrides and last-row offsets.
        const nativeCss = process.env.RME_DASHBOARD_CSS ? fs.readFileSync(process.env.RME_DASHBOARD_CSS,'utf8') : `
          .dashboardGridContainer {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));padding:0}
          .dashboardGridItem * {padding:5px;padding-right:0;font-size:20px;text-align:left;vertical-align:middle}
          .dashboardGridItem.centreInGrid1 {position:relative;left:100%}
          .dashboardGridItem.centreInGrid2 {position:relative;left:50%}
          .dashboard_threeQuarterGauge>svg>path {stroke-width:20px}
          .dashboard-full svg {width:9vw;height:13vh}
          .dashboard-full .dashboardGridItem * {font-size:2.6vmin;padding:.6vmin}
          .dashboard-full .dashboardGridItem svg text {font-size:20px}`;
        await page.setContent(`<style>${nativeCss}</style><style>${fs.readFileSync('octoprint_rme_compatibility/static/css/rme_compatibility.css','utf8')}</style>
          <div id="tab_plugin_dashboard"><div id="native-grid" class="dashboardGridContainer rme-dashboard-heaters">
          <div id="fan" class="dashboardGridItem dashboard_threeQuarterGauge centreInGrid1" data-bind="css: gaugesCentreInGrid('fan')">
          <svg width="140" height="140" viewBox="0 0 140 140"><path class="dashboardGauge" d="M31.82 108.18a54 54 0 1 1 76.36 0" stroke-dasharray="254.47" /></svg></div>
          </div><div class="dashboardProgressContainer"></div></div>`);
        await page.addScriptTag({path:require.resolve('jquery')});
        await page.addScriptTag({path:require.resolve('knockout/build/output/knockout-latest.js')});
        await page.evaluate(() => {window.OCTOPRINT_VIEWMODELS=[];window.gettext=x=>x;window.PNotify=function(){};});
        await page.addScriptTag({path:'octoprint_rme_compatibility/static/js/rme_compatibility.js'});
        await page.waitForFunction(() => OCTOPRINT_VIEWMODELS.length > 0);
        await page.evaluate(() => {
            const s={}; ['dashboard_dock_fan','dashboard_heatbreak_fan','dashboard_buddy_mcu','dashboard_head_board'].forEach(k=>s[k]=ko.observable(true));
            window.vm=new OCTOPRINT_VIEWMODELS[0].construct([{settings:{plugins:{rme_compatibility:s}}},{},{},{}]);
            vm.acceptState({supported:true,connected:true,tool_changes:{active:true,file:'test',remaining:123456,total:200000},
                tune:{updated:Date.now()/1000,dock_fan:255,dock_rpm:100000,heatbreak_pwm:128,heatbreak_rpm:-1,buddy_mcu:123.4,head_board:'unknown'},
                workflow:{workflow:'probing',state:'active',progress:100}});
            vm.renderToolChangeWidget();vm.renderDashboardSensors();
        });
        await page.waitForSelector('#rme-dashboard-progress');
        for (const width of [360,600,1200]) {
            await page.setViewportSize({width,height:900});
            for (const full of [false,true]) {
                await page.evaluate(full=>document.querySelector('#tab_plugin_dashboard').classList.toggle('dashboard-full',full),full);
                const result = await page.evaluate(() => {
                    const grid=document.querySelector('#native-grid'), bounds=grid.getBoundingClientRect();
                    const widgets=[...document.querySelectorAll('.rme-dashboard-gauge')];
                    return {columns:getComputedStyle(grid).gridTemplateColumns.split(' ').length,
                        widgets:widgets.map(w=>{
                            const r=w.getBoundingClientRect(), svg=w.querySelector('svg'), sr=svg.getBoundingClientRect();
                            return {parent:w.parentElement.id,inside:r.left>=bounds.left-1&&r.right<=bounds.right+1,
                                frozen:!!svg.style.width||!!svg.style.height,
                                labels:[...w.querySelectorAll('.rme-gauge-label,.rme-gauge-detail')].every(l=>l.getBoundingClientRect().top>=sr.bottom-1),
                                values:[...svg.querySelectorAll(':scope > text')].every(t=>{
                                    const tr=t.getBoundingClientRect();return tr.left>=sr.left&&tr.right<=sr.right;
                                })};
                        }),fanLeft:getComputedStyle(document.querySelector('#fan')).left};
                });
                assert.equal(result.columns,3,'native grid columns retained');
                assert.equal(result.widgets.length,6);
                assert.equal(result.fanLeft,'0px','native last-row offset cannot overlap injected items');
                result.widgets.forEach(w=>{assert.equal(w.parent,'native-grid');assert(w.inside);assert(!w.frozen);assert(w.labels);assert(w.values);});
            }
        }
        await page.evaluate(()=>vm.acceptState({supported:true,connected:false}));
        await page.waitForFunction(()=>!document.querySelector('.rme-dashboard-gauge'));
        assert.equal(await page.locator('#native-grid').evaluate(g=>g.classList.contains('rme-dashboard-gauge-grid')),false);
        console.log('Native Dashboard grid and nonoverlapping gauge labels: narrow, normal, fullscreen and disconnect passed');
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
