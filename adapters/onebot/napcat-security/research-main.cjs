'use strict';
// Account-free experimental main module. NEVER imports NapCat or QQ login code.
const electron = require('electron');
const {app, session, net} = electron;
const {Worker} = require('node:worker_threads');
const path = require('node:path');
const probe = require('./probe.cjs');
const policy = require('./runtime-policy.cjs');
const workerGuard = require('./worker-guard.cjs');
const mode = process.env.RESEARCH_MODE;
if (!['baseline', 'hardened'].includes(mode)) throw new Error('Explicit RESEARCH_MODE required');
const controlTargets = [
    {host:'expired.badssl.com', expected:'invalid_tls'},
    {host:'wrong.host.badssl.com', expected:'invalid_tls'},
    {host:'self-signed.badssl.com', expected:'invalid_tls'},
];
const allTargets = [...probe.targets(),...controlTargets];
const decisions = [];
const groups = [];
const report = {kind:'runtime-research', mode, versions:process.versions,
    evidence_sha256:policy.EVIDENCE_SHA256, production_ready:false, groups, decisions};
let finished = false;
function finish(error) {
    if (finished) return;
    finished = true;
    clearTimeout(watchdog);
    if (error) report.fatal = String(error.stack || error);
    // Coverage-gap groups are deliberately reported, not hidden in a passing score.
    report.covered_paths_passed = !error && groups.length >= 3
        && groups.filter(g=>g.required).every(g=>g.ok);
    const code = report.covered_paths_passed ? 0 : 1;
    setTimeout(()=>process.exit(code),1000);
    process.stdout.write(JSON.stringify(report)+'\n',()=>app.exit(code));
}
const watchdog = setTimeout(()=>finish(new Error('whole experiment deadline exceeded')),140000);
process.once('uncaughtException', finish);
process.once('unhandledRejection', finish);

function withControlVerdict(target, result) {
    if (target.expected !== 'invalid_tls') return result;
    const error = `${result.code || ''} ${result.error || ''}`;
    const explicit = /\b(CERT_HAS_EXPIRED|ERR_TLS_CERT_ALTNAME_INVALID|DEPTH_ZERO_SELF_SIGNED_CERT|SELF_SIGNED_CERT_IN_CHAIN|ERR_CERT_DATE_INVALID|ERR_CERT_COMMON_NAME_INVALID|ERR_CERT_AUTHORITY_INVALID)\b/.test(error);
    return {...result, verdict: result.outcome === 'http_response' ? 'fail'
        : explicit ? 'pass' : 'inconclusive'};
}
function save(name, required, results) {
    const group = {name,required,ok:results.length===allTargets.length
        && results.every(r=>r.verdict==='pass'),results};
    groups.push(group);
    return group;
}
async function nodeGroup() {
    return Promise.all(allTargets.map(async t=>withControlVerdict(t, await probe.nodeRequest(t,15000))));
}
function chromiumRequest(target,ses) {
    return new Promise(resolve=>{
        let done=false, req;
        const finishRequest = observation=>{
            if(done)return;
            done=true;clearTimeout(timer);
            resolve(withControlVerdict(target,probe.verdict(target,observation)));
            req?.abort();
        };
        const timer=setTimeout(()=>finishRequest({kind:'error',error:{code:'PROBE_TIMEOUT',message:'request deadline'}}),15000);
        try {
            req=net.request({url:'https://'+target.host+'/',session:ses,redirect:'manual',useSessionCookies:false});
            req.on('response',r=>finishRequest({kind:'response',status:r.statusCode}));
            req.on('redirect',status=>finishRequest({kind:'response',status}));
            req.on('error',error=>finishRequest({kind:'error',error}));
            req.end();
        } catch(error) {finishRequest({kind:'error',error});}
    });
}
async function chromiumGroup(ses) {
    // Do not install an observer verify-proc here: it would overwrite the real policy.
    // Bound concurrency: public negative-control endpoints can reset bursts.
    const results = [];
    for (let i = 0; i < allTargets.length; i += 3) {
        results.push(...await Promise.all(allTargets.slice(i, i + 3).map(t=>chromiumRequest(t,ses))));
    }
    return results;
}
function workerGroup(preload) {
    return new Promise((resolve,reject)=>{
        const script = `const tls=require('node:tls');
            const {parentPort,workerData,Worker}=require('node:worker_threads');
            const policy=require(workerData.policy);
            const entryBlocked=policy.filterRoots(tls.getCACertificates('default')).removed.length;
            const p=require(workerData.probe);
            (async()=>{
                if(workerData.expectCleanEntry && entryBlocked!==0){
                    throw new Error('worker entry had unremoved blocked roots: '+entryBlocked);
                }
                if(workerData.nested){
                    const inner = ` + JSON.stringify(
                        "const tls=require('node:tls');" +
                        "const {parentPort,workerData}=require('node:worker_threads');" +
                        "const policy=require(workerData.policy);" +
                        "const innerBlocked=policy.filterRoots(tls.getCACertificates('default')).removed.length;" +
                        "if(workerData.expectCleanEntry && innerBlocked!==0)throw new Error('nested worker entry had unremoved blocked roots: '+innerBlocked);" +
                        "const p=require(workerData.probe);" +
                        "Promise.all(workerData.targets.map(t=>p.nodeRequest(t,15000)))" +
                        ".then(r=>parentPort.postMessage(r)).catch(e=>{throw e});"
                    ) + `;
                    const w=new Worker(inner,{eval:true,workerData});
                    w.on('message',r=>parentPort.postMessage(r));
                    w.on('error',e=>{throw e});
                    w.on('exit',c=>{if(c!==0)throw new Error('nested worker exit '+c);});
                    return;
                }
                const r=await Promise.all(workerData.targets.map(t=>p.nodeRequest(t,15000)));
                parentPort.postMessage(r);
            })().catch(e=>{throw e});`;
        const nested = typeof preload === 'object' && preload !== null ? !!preload.nested : false;
        const explicitPreload = preload === true || (typeof preload === 'object' && preload !== null && !!preload.explicit);
        const expectCleanEntry = mode === 'hardened' || explicitPreload;
        const options={eval:true,workerData:{
            probe:path.join(__dirname,'probe.cjs'),
            policy:path.join(__dirname,'runtime-policy.cjs'),
            targets:allTargets,
            nested,
            expectCleanEntry,
        }};
        if(explicitPreload)options.execArgv=['--require',path.join(__dirname,'node-preload.cjs')];
        // Re-read worker_threads.Worker so hardened mode uses the guarded constructor.
        const WorkerCtor=require('node:worker_threads').Worker;
        const worker=new WorkerCtor(script,options);
        const timer=setTimeout(()=>{worker.terminate();reject(new Error('worker deadline'));},25000);
        worker.on('message',r=>{clearTimeout(timer);resolve(r.map((v,i)=>withControlVerdict(allTargets[i],v)));});
        worker.on('error',e=>{clearTimeout(timer);reject(e);});
        worker.on('exit',code=>{if(code!==0){clearTimeout(timer);reject(new Error('worker exit '+code));}});
    });
}

(async()=>{
    const unsafe=probe.unsafeEnvironment(process.env,process.argv,app);
    if(unsafe.length)throw new Error(unsafe.join('; '));
    let chromium;
    if(mode==='hardened') {
        report.node_policy=policy.installNode();
        report.worker_guard=workerGuard.installWorkerGuard();
        chromium=policy.installChromium(electron,d=>decisions.push(d));
        await chromium.ready;
    } else await app.whenReady();
    save('node-main',true,await nodeGroup());
    save('chromium-default',true,await chromiumGroup(session.defaultSession));
    const newSession=session.fromPartition('research-new-session',{cache:false});
    save('chromium-new-session',true,await chromiumGroup(newSession));
    save('worker-default-no-preload',mode==='hardened',await workerGroup(false));
    if(mode==='hardened') {
        save('worker-nested-default',true,await workerGroup({nested:true}));
        save('worker-explicit-preload',true,await workerGroup(true));
        report.chromium_installed_sessions=chromium.count();
    }
    finish();
})().catch(finish);
