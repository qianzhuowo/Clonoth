'use strict';

// Standalone only: never loads NapCat, QQ profiles, cookies, or account data.
// node probe.cjs [--timeout 20000]
// ELECTRON_RUN_AS_NODE=1 /path/to/electron probe.cjs
// /path/to/electron probe.cjs --chromium
// QQ main/preload embedding: PROBE_AUTORUN=1; PROBE_STACK=chromium selects Chromium.
// --timeout is the whole-run deadline in milliseconds, including app readiness.
const https = require('node:https');

const TRUST_ERRORS = new Set([
    'CERT_REVOKED', 'CERT_UNTRUSTED', 'CERT_REJECTED',
    'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN',
    'UNABLE_TO_GET_ISSUER_CERT', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
    'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
    'ERR_CERT_AUTHORITY_INVALID', 'ERR_CERT_REVOKED',
]);

function targets() {
    const result = [];
    for (const kind of ['dv', 'ov', 'ev']) {
        for (const generation of ['g3b', 'g3c']) {
            for (const aaa of ['', '-aaa']) {
                result.push({
                    host: `${kind}-${generation}${aaa}-emudhra.we-have-a-problem.com`,
                    expected: 'reject',
                });
            }
        }
    }
    return result.concat(['github.com', 'pypi.org'].map(host => ({ host, expected: 'respond' })));
}

function classifyError(error) {
    // Exact allow-list: neither an arbitrary "certificate" string nor ERR_FAILED
    // proves distrust/revocation. Expiry/name mismatch are NOT the targeted issue.
    const code = typeof error === 'string' ? error : String(error?.code || '');
    const message = typeof error === 'string' ? error : String(error?.message || '');
    const tokens = `${code} ${message}`.match(/[A-Z][A-Z0-9_]+/g) || [];
    if (tokens.some(token => ['ETIMEDOUT', 'PROBE_TIMEOUT', 'ERR_TIMED_OUT',
        'ENOTFOUND', 'EAI_AGAIN', 'ERR_NAME_NOT_RESOLVED', 'ECONNRESET',
        'ECONNREFUSED', 'ERR_CONNECTION_RESET', 'ERR_CONNECTION_REFUSED'].includes(token))) {
        return 'inconclusive';
    }
    return tokens.some(token => TRUST_ERRORS.has(token)) ? 'trust_rejection' : 'inconclusive';
}

function verdict(target, observation) {
    if (observation.kind === 'response') {
        return { ...target, verdict: target.expected === 'respond' ? 'pass' : 'fail',
            outcome: 'http_response', status: observation.status };
    }
    const trustedRejection = classifyError(observation.error) === 'trust_rejection';
    return { ...target,
        verdict: target.expected === 'reject' && trustedRejection ? 'pass'
            : trustedRejection ? 'fail' : 'inconclusive',
        outcome: trustedRejection ? 'trust_rejection' : 'transport_error',
        error: String(observation.error?.message || observation.error || 'Unknown error'),
        code: String(observation.error?.code || ''),
    };
}

function parseOptions(argv, env) {
    let timeout = 20000;
    let chromium = env.PROBE_STACK === 'chromium';
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === '--chromium') chromium = true;
        if (arg === '--timeout' || arg.startsWith('--timeout=')) {
            const value = arg === '--timeout' ? argv[++i] : arg.slice('--timeout='.length);
            if (!/^\d+$/.test(value || '')) throw new Error('--timeout must be integer milliseconds');
            timeout = Number(value);
        }
        // QQ/Electron can supply their own switches and the main script path.
    }
    if (!Number.isSafeInteger(timeout) || timeout < 100 || timeout > 300000) {
        throw new Error('--timeout must be between 100 and 300000 milliseconds');
    }
    return { timeout, chromium };
}

function unsafeEnvironment(env, argv, app) {
    const reasons = [];
    // No additional root bundle is assumed. Fail closed rather than presenting
    // results from externally customized roots as proof of bundled-CA safety.
    for (const key of ['NODE_EXTRA_CA_CERTS', 'NODE_OPTIONS', 'SSL_CERT_FILE', 'SSL_CERT_DIR']) {
        if (env[key]) reasons.push(`${key} is set; use a clean probe environment`);
    }
    if (env.NODE_TLS_REJECT_UNAUTHORIZED === '0') reasons.push('NODE_TLS_REJECT_UNAUTHORIZED=0');
    for (const name of ['ignore-certificate-errors', 'ignore-certificate-errors-spki-list',
        'allow-insecure-localhost', 'use-openssl-ca', 'use-system-ca']) {
        if (argv.some(arg => arg === `--${name}` || arg.startsWith(`--${name}=`))
            || app?.commandLine?.hasSwitch(name)) reasons.push(`unsafe/custom TLS switch: --${name}`);
    }
    return reasons;
}

function nodeRequest(target, timeout) {
    return new Promise(resolve => {
        let done = false;
        let request;
        const finish = observation => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            resolve(verdict(target, observation));
            request?.destroy();
        };
        // Independent timer includes DNS lookup and TCP/TLS connection setup;
        // socket inactivity timeout alone does not cover DNS hangs.
        const timer = setTimeout(() => finish({ kind: 'error',
            error: { code: 'PROBE_TIMEOUT', message: 'PROBE_TIMEOUT: whole request deadline' } }), timeout);
        try {
            request = https.request({ hostname: target.host, port: 443, path: '/', method: 'GET',
                agent: false, rejectUnauthorized: true, timeout,
                headers: { 'User-Agent': 'NapCat-TLS-security-probe/1', Connection: 'close' },
            }, response => {
                finish({ kind: 'response', status: response.statusCode });
                response.destroy();
            });
            request.on('timeout', () => finish({ kind: 'error',
                error: { code: 'PROBE_TIMEOUT', message: 'PROBE_TIMEOUT: socket inactivity' } }));
            request.on('error', error => finish({ kind: 'error', error }));
            request.end();
        } catch (error) {
            finish({ kind: 'error', error });
        }
    });
}

function chromiumRequest(target, timeout, electron, partition) {
    return new Promise(resolve => {
        let done = false;
        let request;
        let verificationError = null;
        const isolated = electron.session.fromPartition(partition, { cache: false });
        // A fresh non-persistent session per host: no cookies, cached certificates,
        // connections, or data from the QQ/default session are consulted.
        isolated.setCertificateVerifyProc((details, callback) => {
            if (details.hostname === target.host
                && classifyError(details.verificationResult) === 'trust_rejection') {
                verificationError = details.verificationResult;
            }
            callback(-3); // ALWAYS use Chromium's default verification; never allow/override.
        });
        const finish = observation => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            resolve(verdict(target, observation));
            request?.abort();
        };
        const timer = setTimeout(() => finish({ kind: 'error',
            error: { code: 'PROBE_TIMEOUT', message: 'PROBE_TIMEOUT: Chromium request deadline' } }), timeout);
        try {
            request = electron.net.request({ url: `https://${target.host}/`, method: 'GET',
                session: isolated, redirect: 'manual', useSessionCookies: false });
            request.setHeader('Cache-Control', 'no-cache, no-store');
            request.setHeader('Pragma', 'no-cache');
            request.on('response', response => finish({ kind: 'response', status: response.statusCode }));
            // A redirect is already a successful TLS/HTTP response; no need to
            // follow another host or demand HTTP 200 from normal control sites.
            request.on('redirect', (status, _method, _url, _headers) =>
                finish({ kind: 'response', status }));
            request.on('error', error => {
                // Electron sometimes masks a cert rejection as ERR_FAILED. Only
                // its explicit default verifier result can disambiguate that case.
                const masked = /\bERR_FAILED\b/.test(String(error.message));
                finish({ kind: 'error', error: masked && verificationError
                    ? new Error(verificationError) : error });
            });
            request.end();
        } catch (error) {
            finish({ kind: 'error', error });
        }
    });
}

function electronRuntime() {
    if (!process.versions.electron) return null;
    try {
        const electron = require('electron');
        return electron && electron.app ? electron : null;
    } catch (_) {
        return null;
    }
}

async function main() {
    const electron = electronRuntime();
    const app = electron?.app;
    const started = Date.now();
    const allTargets = targets();
    const results = new Map();
    const report = {
        kind: 'summary',
        version: { probe: 1, node: process.versions.node,
            electron: process.versions.electron || null, chrome: process.versions.chrome || null,
            openssl: process.versions.openssl || null },
        stack: 'node:https', timeout_ms: null, ok: false, results: [],
    };
    let finished = false;
    let watchdog;
    function finish(fatal) {
        if (finished) return;
        finished = true;
        clearTimeout(watchdog);
        if (fatal) report.fatal = String(fatal.message || fatal);
        report.results = allTargets.map(target => results.get(target.host) || {
            ...target, verdict: 'inconclusive', outcome: 'not_completed',
            error: report.fatal || 'Probe did not complete',
        });
        report.duration_ms = Date.now() - started;
        report.ok = !fatal && report.results.every(result => result.verdict === 'pass');
        const code = report.ok ? 0 : 1;
        const exit = () => { if (app) app.exit(code); else process.exit(code); };
        // Flush JSON before exit, with a bounded fallback if stdout itself stalls.
        setTimeout(() => process.exit(code), 1000);
        process.stdout.write(`${JSON.stringify(report)}\n`, exit);
    }
    process.once('uncaughtException', error => finish(error));
    process.once('unhandledRejection', error => finish(error));
    try {
        const options = parseOptions(process.argv.slice(1), process.env);
        report.stack = options.chromium ? 'electron:net.request' : 'node:https';
        report.timeout_ms = options.timeout;
        watchdog = setTimeout(() => finish(new Error('PROBE_TIMEOUT: overall probe deadline exceeded')),
            options.timeout);
        const unsafe = unsafeEnvironment(process.env, process.argv, app);
        if (unsafe.length) throw new Error(unsafe.join('; '));
        if (options.chromium) {
            if (!app) throw new Error('--chromium requires Electron app mode, not ELECTRON_RUN_AS_NODE');
            await app.whenReady();
        }
        const remaining = Math.max(1, options.timeout - (Date.now() - started));
        const partition = `napcat-tls-probe-${process.pid}-${Date.now()}`;
        await Promise.all(allTargets.map(async (target, index) => {
            const result = options.chromium
                ? await chromiumRequest(target, remaining, electron, `${partition}-${index}`)
                : await nodeRequest(target, remaining);
            results.set(target.host, result);
        }));
        finish();
    } catch (error) {
        finish(error);
    }
}

module.exports = { targets, classifyError, verdict, parseOptions, unsafeEnvironment,
    nodeRequest, chromiumRequest };
if (require.main === module || process.env.PROBE_AUTORUN === '1') main();
