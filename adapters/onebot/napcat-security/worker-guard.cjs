'use strict';
// EXPERIMENTAL: Worker thread TLS policy guard for embedded Node 24+.
// Why needed: tls.setDefaultCACertificates() in the main/utility thread does NOT
// propagate to newly created node:worker_threads.Worker instances by default.
//
// Explicit non-goals / security boundaries:
// 1. Does NOT defend against arbitrary same-process malicious tampering.
// 2. Does NOT cover pre-saved references to node:worker_threads.Worker captured
//    before installWorkerGuard() runs.
// 3. Does NOT cover native libraries/addons (e.g. QQ major.node / C++ pthreads).
// 4. Does NOT override callers that explicitly pass custom `ca` or `secureContext`
//    to tls.connect / https.request / tls.createSecureContext.

const fs = require('node:fs');
const moduleBuiltin = require('node:module');
const path = require('node:path');
const tls = require('node:tls');
const workerThreads = require('node:worker_threads');
const policy = require('./runtime-policy.cjs');

const PRELOAD_PATH = path.resolve(__dirname, 'node-preload.cjs');
const GUARD_SYMBOL = Symbol.for('clonoth.tls.workerGuardInstalled');

// Exact verified host flags that Electron/QQ may inject into process.execArgv
// which Node's Worker constructor rejects with ERR_WORKER_INVALID_EXEC_ARGV.
const KNOWN_HOST_ONLY_FLAGS = new Set([
    '--no-sandbox',
]);

function hasNonEmptyEnvValue(env, key) {
    if (!env || typeof env !== 'object') return false;
    const val = env[key];
    return val !== undefined && val !== null && String(val).trim() !== '';
}

function assertCleanTlsEnv(env, contextLabel) {
    if (!env || typeof env !== 'object') return;
    if (String(env.NODE_TLS_REJECT_UNAUTHORIZED) === '0') {
        throw new Error(`Unsafe TLS override in ${contextLabel}: NODE_TLS_REJECT_UNAUTHORIZED=0`);
    }
    // NODE_OPTIONS --require executes BEFORE options.execArgv --require in Worker threads,
    // which would allow code execution before our TLS preload. Fail closed on non-empty NODE_OPTIONS.
    if (hasNonEmptyEnvValue(env, 'NODE_OPTIONS')) {
        throw new Error(`Unsafe NODE_OPTIONS in ${contextLabel}: non-empty NODE_OPTIONS is forbidden`);
    }
    if (hasNonEmptyEnvValue(env, 'NODE_EXTRA_CA_CERTS')) {
        throw new Error(`Unsafe NODE_EXTRA_CA_CERTS in ${contextLabel}: custom CA env is forbidden`);
    }
}

function assertRuntimeApis(deps = {}) {
    const tlsMod = deps.tls || tls;
    const moduleMod = deps.module || moduleBuiltin;
    const wtMod = deps.workerThreads || workerThreads;
    const env = deps.env || process.env;
    const preloadPath = deps.preloadPath || PRELOAD_PATH;

    assertCleanTlsEnv(env, 'process.env');
    if (typeof tlsMod.getCACertificates !== 'function'
        || typeof tlsMod.setDefaultCACertificates !== 'function') {
        throw new Error('Required Node CA APIs unavailable for worker guard; failing closed');
    }
    if (!wtMod || typeof wtMod.Worker !== 'function') {
        throw new Error('node:worker_threads.Worker unavailable; failing closed');
    }
    if (!moduleMod || typeof moduleMod.syncBuiltinESMExports !== 'function') {
        throw new Error('node:module.syncBuiltinESMExports unavailable; failing closed');
    }
    if (!path.isAbsolute(preloadPath) || !fs.existsSync(preloadPath)) {
        throw new Error(`Required worker preload missing: ${preloadPath}`);
    }
    return { tlsMod, moduleMod, wtMod, env, preloadPath };
}

function stripLeadingPreload(execArgv, preloadPath) {
    const out = [];
    for (let i = 0; i < execArgv.length; i++) {
        const arg = execArgv[i];
        if ((arg === '--require' || arg === '-r') && i + 1 < execArgv.length
            && path.resolve(String(execArgv[i + 1])) === preloadPath) {
            i++;
            continue;
        }
        if (typeof arg === 'string' && arg.startsWith('--require=')
            && path.resolve(arg.slice('--require='.length)) === preloadPath) {
            continue;
        }
        out.push(arg);
    }
    return out;
}

function filterInheritedExecArgv(parentExecArgv) {
    if (!Array.isArray(parentExecArgv)) return [];
    const out = [];
    for (const raw of parentExecArgv) {
        const arg = String(raw);
        if (KNOWN_HOST_ONLY_FLAGS.has(arg)) {
            continue;
        }
        out.push(arg);
    }
    return out;
}

function buildWorkerOptions(options, preloadPath, parentExecArgv = process.execArgv, parentEnv = process.env) {
    if (options !== undefined && (typeof options !== 'object' || options === null || Array.isArray(options))) {
        const err = new TypeError('The "options" argument must be of type object');
        err.code = 'ERR_INVALID_ARG_TYPE';
        throw err;
    }
    const rawOptions = options || {};
    if (rawOptions.execArgv !== undefined && !Array.isArray(rawOptions.execArgv)) {
        const err = new TypeError('The "options.execArgv" property must be an instance of Array');
        err.code = 'ERR_INVALID_ARG_TYPE';
        throw err;
    }

    assertCleanTlsEnv(parentEnv, 'process.env');
    if (rawOptions.env !== undefined && rawOptions.env !== null && rawOptions.env !== workerThreads.SHARE_ENV) {
        if (typeof rawOptions.env !== 'object' || Array.isArray(rawOptions.env)) {
            const err = new TypeError('The "options.env" property must be an instance of Object, undefined, null, or worker_threads.SHARE_ENV');
            err.code = 'ERR_INVALID_ARG_TYPE';
            throw err;
        }
        assertCleanTlsEnv(rawOptions.env, 'options.env');
    }

    const hasExplicitExecArgv = rawOptions.execArgv !== undefined;
    const baseExecArgv = hasExplicitExecArgv
        ? Array.from(rawOptions.execArgv, String)
        : filterInheritedExecArgv(parentExecArgv);

    // Required security preload MUST execute before any caller-supplied preload or worker entry,
    // while preserving all other valid parent/caller flags (such as legitimate --require / --import).
    const mergedExecArgv = ['--require', preloadPath, ...stripLeadingPreload(baseExecArgv, preloadPath)];

    return {
        ...rawOptions,
        execArgv: mergedExecArgv,
    };
}

function createGuardedWorkerClass(OriginalWorker, runtime) {
    const { tlsMod, preloadPath } = runtime;
    class GuardedWorker extends OriginalWorker {
        constructor(filename, options, ...rest) {
            // Fail closed on any extra constructor arguments (>= 3 args).
            // Never allow a 3rd argument to bypass preload injection.
            if (rest.length > 0) {
                const err = new TypeError('Unsupported extra arguments passed to guarded Worker constructor; failing closed');
                err.code = 'ERR_INVALID_ARG_VALUE';
                throw err;
            }
            assertCleanTlsEnv(process.env, 'process.env');
            if (typeof tlsMod.getCACertificates !== 'function'
                || typeof tlsMod.setDefaultCACertificates !== 'function') {
                throw new Error('Required Node CA APIs unavailable on Worker creation; failing closed');
            }
            const guardedOptions = buildWorkerOptions(options, preloadPath, process.execArgv, process.env);
            super(filename, guardedOptions);
        }
    }
    Object.defineProperty(GuardedWorker, 'name', { value: 'Worker', configurable: true });
    Object.defineProperty(GuardedWorker, GUARD_SYMBOL, {
        value: { OriginalWorker, preloadPath },
        enumerable: false,
        configurable: false,
        writable: false,
    });
    return GuardedWorker;
}

function installWorkerGuard(deps = {}) {
    const runtime = assertRuntimeApis(deps);
    const { tlsMod, moduleMod, wtMod, preloadPath } = runtime;

    // Ensure the current thread itself has the subtracted default CA store before spawning workers.
    const currentDefault = tlsMod.getCACertificates('default');
    const filtered = policy.filterRoots(currentDefault);
    if (filtered.removed.length > 0) {
        if (deps.tls) {
            tlsMod.setDefaultCACertificates(filtered.retained);
            if (policy.filterRoots(tlsMod.getCACertificates('default')).removed.length > 0) {
                throw new Error('Node CA replacement did not take effect');
            }
        } else {
            policy.installNode();
        }
    }

    const currentWorker = wtMod.Worker;
    if (currentWorker && currentWorker[GUARD_SYMBOL]) {
        moduleMod.syncBuiltinESMExports();
        return {
            kind: 'worker-guard',
            installed: true,
            already_installed: true,
            preload_path: currentWorker[GUARD_SYMBOL].preloadPath,
            thread_id: wtMod.threadId,
            is_main_thread: wtMod.isMainThread,
        };
    }

    const GuardedWorker = createGuardedWorkerClass(currentWorker, runtime);
    wtMod.Worker = GuardedWorker;
    moduleMod.syncBuiltinESMExports();

    if (wtMod.Worker !== GuardedWorker) {
        throw new Error('Failed to install GuardedWorker on node:worker_threads');
    }

    return {
        kind: 'worker-guard',
        installed: true,
        already_installed: false,
        preload_path: preloadPath,
        thread_id: wtMod.threadId,
        is_main_thread: wtMod.isMainThread,
    };
}

module.exports = {
    PRELOAD_PATH,
    GUARD_SYMBOL,
    KNOWN_HOST_ONLY_FLAGS,
    filterInheritedExecArgv,
    buildWorkerOptions,
    installWorkerGuard,
};
