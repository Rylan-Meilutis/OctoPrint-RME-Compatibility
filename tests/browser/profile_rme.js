// Real Chromium CPU profile with OctoPrint's actual KO/jQuery/PNotify libraries.
// NODE_PATH=<playwright modules> node tests/browser/profile_rme.js [git revision]
// OCTOPRINT_STATIC points at an OctoPrint checkout's src/octoprint/static.
const fs = require('fs'), path = require('path'), cp = require('child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const staticRoot = process.env.OCTOPRINT_STATIC || '/tmp/rme-octoprint-reference/src/octoprint/static';
const revision = process.argv[2];
const uiRoot = process.env.RME_PROFILE_UI_CUSTOMIZER;
let backup = null;
if (process.env.RME_PROFILE_BACKUP) {
    // Only whitelist UI settings and RME state. Never restore credentials,
    // users, API keys, printer connections, or external service settings.
    const script = 'import sys,zipfile,yaml,json; z=zipfile.ZipFile(sys.argv[1]); c=yaml.safe_load(z.read("basedir/config.yaml")); print(json.dumps({"ui":c.get("plugins",{}).get("uicustomizer",{}),"state":json.loads(z.read("basedir/data/rme_compatibility/state.json")),"plugins":list(c.get("plugins",{}))}))';
    try { backup = JSON.parse(cp.execFileSync('python3', ['-c', script, process.env.RME_PROFILE_BACKUP], {encoding: 'utf8'})); }
    catch (error) { if (error.status !== 0 || !error.stdout) throw error; backup = JSON.parse(error.stdout); }
}
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
            '</div><div id="settings_dialog" class="modal hide fade" tabindex="-1">' +
            '<div class="modal-header"><h3>Settings</h3></div><div class="modal-body">' +
            '<div id="settings">' + render('rme_compatibility_settings.jinja2') +
            '</div></div><div class="modal-footer"><button class="btn" data-dismiss="modal">Close</button></div></div></body>');
        for (const css of ['bootstrap.css', 'bootstrap-modal.css', 'pnotify.core.min.css']) await page.addStyleTag({path: path.join(staticRoot, 'css', css)});
        await page.addStyleTag({path: path.join(root, 'octoprint_rme_compatibility/static/css/rme_compatibility.css')});
        for (const js of ['jquery/jquery.js', 'knockout.js', 'bootstrap/bootstrap.js',
            'bootstrap/bootstrap-modalmanager.js', 'bootstrap/bootstrap-modal.js', 'pnotify/pnotify.core.min.js'])
            await page.addScriptTag({path: path.join(staticRoot, 'js/lib', js)});
        if (uiRoot) {
            await page.addScriptTag({path: path.join(staticRoot, 'js/lib/lodash.js')});
            await page.addScriptTag({path: path.join(staticRoot, 'js/lib/knockout.mapping-latest.js')});
            await page.addStyleTag({path: path.join(uiRoot, 'static/css/uicustomizer.css')});
        }
        await page.evaluate(() => {
            window.OCTOPRINT_VIEWMODELS = []; window.gettext = x => x;
            window.BASEURL = '/'; window.OctoPrint = {simpleApiCommand: () => $.Deferred().resolve().promise()};
        });
        await page.addScriptTag({content: source});
        await page.waitForFunction(() => OCTOPRINT_VIEWMODELS.length);
        if (uiRoot) {
            await page.evaluate(() => { window.Modernizr = {localstorage: false}; });
            await page.addScriptTag({path: path.join(uiRoot, 'static/js/Sortable.min.js')});
            await page.addScriptTag({path: path.join(uiRoot, 'static/js/uicustomizer.js')});
            const template = fs.readFileSync(path.join(uiRoot, 'templates/uicustomizer_settings.jinja2'), 'utf8')
                .replace(/{{\s*_\('([^']*)'\)\s*}}/g, '$1');
            await page.evaluate(html => $('#settings_dialog .modal-body').append('<div id="settings_plugin_uicustomizer">' + html + '</div>'), template);
            await page.waitForFunction(() => OCTOPRINT_VIEWMODELS.length > 1);
        }
        await page.evaluate(({backup, useUi}) => {
            const defaults = {firmware_variant: ko.observable('coreone_indx')};
            window.vm = new OCTOPRINT_VIEWMODELS[0].construct([{settings: {plugins: {rme_compatibility: defaults}}}, {isUser: ko.observable(true)}, {}, {}]);
            ko.applyBindings(vm, document.getElementById('workspace'));
            ko.applyBindings(vm, document.getElementById('settings'));
            vm.onAllBound();
            window.hookTimings = [];
            if (useUi) {
                const settings = {plugins: {uicustomizer: ko.mapping.fromJS(backup ? backup.ui : {rows: [], mainTabs: [], topIconSort: []})}};
                window.OctoPrint.coreui = {viewmodels: {pluginManagerViewModel: {plugins: {allItems: (backup ? backup.plugins : [])
                    .map(key => ({key, name: key, enabled: true}))}}}};
                window.uic = new OCTOPRINT_VIEWMODELS[1].construct([{settings}, {}]);
                uic.settings = settings; uic.UICsettings = settings.plugins.uicustomizer;
            }
            // Exercise OctoPrint's modal manager/layout, not a div.show(). This
            // invokes the same show/hidden hooks as SettingsViewModel. Other
            // installed plugins are deliberately not simulated here.
            $.fn.modal.defaults.maxHeight = () => $(window).height() - 100;
            $('#settings_dialog').on('show', event => {
                if (event.target.id === 'settings_dialog') {
                    for (const model of [vm, window.uic].filter(Boolean)) {
                        const start = performance.now(); model.onSettingsShown();
                        hookTimings.push({model: model === vm ? 'RME' : 'UI Customizer', ms: performance.now() - start});
                    }
                }
            });
            document.getElementById('open-settings').onclick = () => {
                window.settingsShown = new Promise(resolve => $('#settings_dialog').one('shown', resolve));
                $('#settings_dialog').modal({minHeight: () => Math.max($.fn.modal.defaults.maxHeight() - 80, 250)})
                    .css({'margin-left': function () { return -($(this).width() / 2); }});
            };
            window.snapshot = {connected: true, supported: true, machine: {logical_tools: 8, tool_capacity: 8},
                loaded_filaments: Array.from({length: 8}, (_, tool) => ({tool, material: 'PLA', color: '#ffffff', profile: 'PLA-00' + tool})),
                spoolmanager: {provider: 'spoolmanager', inventory: Array.from({length: 1000}, (_, i) =>
                    ({database_id: i + 1, material: 'PLA', color: '#ffffff', display_name: 'Spool ' + i}))}};
            if (backup) window.snapshot = Object.assign({}, backup.state, {connected: true, supported: true});
            vm.acceptState(JSON.parse(JSON.stringify(snapshot)));
            vm.command = () => $.Deferred().resolve().promise();
            vm.showFilamentMapping();
            window.longTasks = [];
            new PerformanceObserver(list => longTasks.push(...list.getEntries().map(e => e.duration))).observe({type: 'longtask', buffered: false});
        }, {backup, useUi: !!uiRoot});
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
            await window.settingsShown;
            const settingsShownMs = performance.now() - started;
            observer.disconnect(); clearInterval(heartbeat);
            return {maxUpdateMs: Math.max(...durations), totalUpdateMs: durations.reduce((a,b) => a+b,0),
                settingsOpenMs, settingsShownMs, maxHeartbeatMs: maxHeartbeat, mutations, longTasks: window.longTasks,
                nodes: document.querySelectorAll('*').length, notices: document.querySelectorAll('.ui-pnotify').length, hookTimings: window.hookTimings};
        });
        const {profile} = await session.send('Profiler.stop');
        const output = '/tmp/rme-browser-modal-' + (revision || 'working') + (process.env.RME_PROFILE_CPU_RATE ? '-cpu' + process.env.RME_PROFILE_CPU_RATE : '');
        fs.writeFileSync(output + '.cpuprofile', JSON.stringify(profile));
        fs.writeFileSync(output + '.json', JSON.stringify({...result, errors}, null, 2));
        console.log(JSON.stringify({revision: revision || 'working', ...result, errors}, null, 2));
        if (errors.length) throw new Error('Browser errors: ' + errors.join('; '));
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
