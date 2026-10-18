// Real Chromium CPU profile with OctoPrint's actual KO/jQuery/PNotify libraries.
// NODE_PATH=<playwright modules> node tests/browser/profile_rme.js [git revision]
// OCTOPRINT_STATIC points at an OctoPrint checkout's src/octoprint/static.
const fs = require('fs'), path = require('path'), cp = require('child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const staticRoot = process.env.OCTOPRINT_STATIC || '/tmp/rme-octoprint-reference/src/octoprint/static';
const revision = process.argv[2];
const sourcePath = 'octoprint_rme_compatibility/static/js/rme_compatibility.js';
let source = fs.readFileSync(path.join(root, sourcePath), 'utf8');
if (revision) {
    try { source = cp.execFileSync('git', ['show', revision + ':' + sourcePath], {cwd: root, encoding: 'utf8'}); }
    catch (error) { if (error.status !== 0 || !error.stdout) throw error; source = error.stdout; }
}
function render(name) {
    return fs.readFileSync(path.join(root, 'octoprint_rme_compatibility/templates', name), 'utf8')
        .replace(/{% include "([^"]+)" %}/g, (_, child) => render(child));
}
(async () => {
    const browser = await chromium.launch({headless: true});
    try {
        const page = await browser.newPage({viewport: {width: 1280, height: 900}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.setContent('<body><button id="open-settings">Settings</button><div id="state"><div class="progress"></div></div>' +
            '<div id="tab_spoolOverview"></div><div id="workspace">' + render('rme_compatibility_tab.jinja2') +
            '</div><div id="settings" style="display:none">' + render('rme_compatibility_settings.jinja2') + '</div></body>');
        for (const css of ['bootstrap.css', 'pnotify.core.min.css']) await page.addStyleTag({path: path.join(staticRoot, 'css', css)});
        await page.addStyleTag({path: path.join(root, 'octoprint_rme_compatibility/static/css/rme_compatibility.css')});
        for (const js of ['jquery/jquery.js', 'knockout.js', 'bootstrap/bootstrap.js', 'pnotify/pnotify.core.min.js'])
            await page.addScriptTag({path: path.join(staticRoot, 'js/lib', js)});
        await page.evaluate(() => {
            window.OCTOPRINT_VIEWMODELS = []; window.gettext = x => x;
            window.BASEURL = '/'; window.OctoPrint = {simpleApiCommand: () => $.Deferred().resolve().promise()};
        });
        await page.addScriptTag({content: source});
        await page.waitForFunction(() => OCTOPRINT_VIEWMODELS.length);
        await page.evaluate(() => {
            const defaults = {firmware_variant: ko.observable('coreone_indx')};
            window.vm = new OCTOPRINT_VIEWMODELS[0].construct([{settings: {plugins: {rme_compatibility: defaults}}}, {isUser: ko.observable(true)}, {}, {}]);
            ko.applyBindings(vm, document.getElementById('workspace'));
            ko.applyBindings(vm, document.getElementById('settings'));
            vm.onAllBound();
            document.getElementById('open-settings').onclick = () => { $('#workspace').hide(); $('#settings').show(); };
            window.snapshot = {connected: true, supported: true, machine: {logical_tools: 8, tool_capacity: 8},
                loaded_filaments: Array.from({length: 8}, (_, tool) => ({tool, material: 'PLA', color: '#ffffff', profile: 'PLA-00' + tool})),
                spoolmanager: {provider: 'spoolmanager', inventory: Array.from({length: 1000}, (_, i) =>
                    ({database_id: i + 1, material: 'PLA', color: '#ffffff', display_name: 'Spool ' + i}))}};
            vm.acceptState(JSON.parse(JSON.stringify(snapshot)));
            vm.command = () => $.Deferred().resolve().promise();
            vm.showFilamentMapping();
            window.longTasks = [];
            new PerformanceObserver(list => longTasks.push(...list.getEntries().map(e => e.duration))).observe({type: 'longtask', buffered: false});
        });
        const session = await page.context().newCDPSession(page);
        if (process.env.RME_PROFILE_CPU_RATE) await session.send('Emulation.setCPUThrottlingRate', {rate: Number(process.env.RME_PROFILE_CPU_RATE)});
        await session.send('Profiler.enable'); await session.send('Profiler.start');
        const result = await page.evaluate(async () => {
            let mutations = 0, maxHeartbeat = 0, last = performance.now();
            const observer = new MutationObserver(records => mutations += records.length);
            observer.observe(document.body, {childList: true, subtree: true, attributes: true});
            const heartbeat = setInterval(() => { const now = performance.now(); maxHeartbeat = Math.max(maxHeartbeat, now - last); last = now; }, 16);
            const durations = [];
            for (let i = 0; i < 60; i++) {
                snapshot.firmware = {offset: i}; snapshot.spoolmanager.status = 'Poll ' + i;
                snapshot.workflow = {type: 'error', workflow: 'printer', state: 'waiting', message: 'Check filament', seq: i, received_at: i};
                const start = performance.now(); vm.acceptState(JSON.parse(JSON.stringify(snapshot))); durations.push(performance.now() - start);
                await new Promise(resolve => setTimeout(resolve, 30));
            }
            const started = performance.now(); document.getElementById('open-settings').click();
            await new Promise(requestAnimationFrame);
            const settingsOpenMs = performance.now() - started;
            observer.disconnect(); clearInterval(heartbeat);
            return {maxUpdateMs: Math.max(...durations), totalUpdateMs: durations.reduce((a,b) => a+b,0),
                settingsOpenMs, maxHeartbeatMs: maxHeartbeat, mutations, longTasks: window.longTasks,
                nodes: document.querySelectorAll('*').length, notices: document.querySelectorAll('.ui-pnotify').length};
        });
        const {profile} = await session.send('Profiler.stop');
        const output = '/tmp/rme-browser-' + (revision || 'working') + (process.env.RME_PROFILE_CPU_RATE ? '-cpu' + process.env.RME_PROFILE_CPU_RATE : '');
        fs.writeFileSync(output + '.cpuprofile', JSON.stringify(profile));
        fs.writeFileSync(output + '.json', JSON.stringify({...result, errors}, null, 2));
        console.log(JSON.stringify({revision: revision || 'working', ...result, errors}, null, 2));
        if (errors.length) throw new Error('Browser errors: ' + errors.join('; '));
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
