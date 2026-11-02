"""Offline and real Worker execution tests for worker-guard.cjs and bootstrap.mjs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "napcat-security"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node 24+ required")
class WorkerGuardTests(unittest.TestCase):
    def clean_env(self):
        env = dict(os.environ)
        for k in ["PROBE_AUTORUN", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS",
                  "NODE_TLS_REJECT_UNAUTHORIZED", "CLONOTH_TLS_SELFTEST",
                  "CLONOTH_TLS_SELFTEST_WORKER"]:
            env.pop(k, None)
        return env

    def run_node(self, script, args=None, is_esm=False):
        env = self.clean_env()
        if is_esm:
            with tempfile.TemporaryDirectory() as tmp:
                entry = Path(tmp) / "entry.mjs"
                entry.write_text(script, encoding="utf-8")
                cmd = [NODE, str(entry), str(ROOT), *(args or [])]
                proc = subprocess.run(
                    cmd,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=20,
                    env=env,
                )
        else:
            cmd = [NODE, "-", str(ROOT), *(args or [])]
            proc = subprocess.run(
                cmd,
                input=script,
                text=True,
                capture_output=True,
                check=True,
                timeout=20,
                env=env,
            )
        return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr

    def test_uninitialized_worker_vs_guarded_default_worker(self):
        script = r"""
        const path = require('node:path');
        const tls = require('node:tls');
        const root = process.argv[2];
        const policy = require(path.join(root, 'runtime-policy.cjs'));
        const guard = require(path.join(root, 'worker-guard.cjs'));

        const probeScript = `
            const tls = require('node:tls');
            const path = require('node:path');
            const { parentPort, workerData } = require('node:worker_threads');
            const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));
            const certs = tls.getCACertificates('default');
            parentPort.postMessage({
                count: certs.length,
                remainingBlocked: policy.filterRoots(certs).removed.length,
            });
        `;

        function runWorker(WorkerCtor) {
            return new Promise((resolve, reject) => {
                const w = new WorkerCtor(probeScript, { eval: true, workerData: { root } });
                w.on('message', resolve);
                w.on('error', reject);
            });
        }

        (async () => {
            const RawWorker = require('node:worker_threads').Worker;
            policy.installNode();
            const beforeGuard = await runWorker(RawWorker);
            const state = guard.installWorkerGuard();
            const idempotent = guard.installWorkerGuard();
            const GuardedWorker = require('node:worker_threads').Worker;
            const afterGuard = await runWorker(GuardedWorker);
            console.log(JSON.stringify({
                beforeGuard,
                afterGuard,
                installed: state.installed,
                already_installed: idempotent.already_installed,
            }));
        })();
        """
        result, _ = self.run_node(script)
        self.assertGreaterEqual(result["beforeGuard"]["remainingBlocked"], 1)
        self.assertEqual(result["afterGuard"]["remainingBlocked"], 0)
        self.assertLess(result["afterGuard"]["count"], result["beforeGuard"]["count"])
        self.assertTrue(result["installed"])
        self.assertTrue(result["already_installed"])

    def test_cjs_esm_file_url_eval_and_data_url_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cjs_worker = tmp_path / "worker.cjs"
            esm_worker = tmp_path / "worker.mjs"

            cjs_worker.write_text(
                "const tls = require('node:tls');\n"
                "const path = require('node:path');\n"
                "const { parentPort, workerData } = require('node:worker_threads');\n"
                "const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));\n"
                "const certs = tls.getCACertificates('default');\n"
                "parentPort.postMessage({ mode: 'cjs-file', blocked: policy.filterRoots(certs).removed.length });\n",
                encoding="utf-8",
            )
            esm_worker.write_text(
                "import tls from 'node:tls';\n"
                "import path from 'node:path';\n"
                "import { createRequire } from 'node:module';\n"
                "import { parentPort, workerData } from 'node:worker_threads';\n"
                "const require = createRequire(import.meta.url);\n"
                "const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));\n"
                "const certs = tls.getCACertificates('default');\n"
                "parentPort.postMessage({ mode: 'esm-file', blocked: policy.filterRoots(certs).removed.length });\n",
                encoding="utf-8",
            )

            script = r"""
            import path from 'node:path';
            import { pathToFileURL } from 'node:url';
            import { createRequire } from 'node:module';
            const require = createRequire(import.meta.url);
            const root = process.argv[2];
            const cjsPath = process.argv[3];
            const esmPath = process.argv[4];

            require(path.join(root, 'worker-guard.cjs')).installWorkerGuard();
            // Dynamic ESM import verifies syncBuiltinESMExports() updated ESM binding.
            const { Worker } = await import('node:worker_threads');

            function spawn(target, options = {}) {
                return new Promise((resolve, reject) => {
                    const w = new Worker(target, { workerData: { root }, ...options });
                    w.on('message', m => resolve({ ...m, instanceofWorker: w instanceof Worker }));
                    w.on('error', reject);
                });
            }

            const evalCjs = await spawn(
                `const tls=require('node:tls'),path=require('node:path'),{parentPort,workerData}=require('node:worker_threads');` +
                `const policy=require(path.join(workerData.root,'runtime-policy.cjs'));` +
                `parentPort.postMessage({mode:'eval-cjs',blocked:policy.filterRoots(tls.getCACertificates('default')).removed.length});`,
                { eval: true }
            );
            const evalEsm = await spawn(
                `import tls from 'node:tls';import path from 'node:path';import {createRequire} from 'node:module';` +
                `import {parentPort,workerData} from 'node:worker_threads';` +
                `const require=createRequire(import.meta.url);const policy=require(path.join(workerData.root,'runtime-policy.cjs'));` +
                `parentPort.postMessage({mode:'eval-esm',blocked:policy.filterRoots(tls.getCACertificates('default')).removed.length});`,
                { eval: true, execArgv: ['--input-type=module'] }
            );
            const cjsFile = await spawn(cjsPath);
            const esmFile = await spawn(esmPath);
            const fileUrl = await spawn(pathToFileURL(esmPath));
            const dataUrlScript =
                `import tls from 'node:tls';import {createRequire} from 'node:module';import {parentPort,workerData} from 'node:worker_threads';` +
                `const req=createRequire(workerData.policyUrl);const policy=req('./runtime-policy.cjs');` +
                `parentPort.postMessage({mode:'data-url',blocked:policy.filterRoots(tls.getCACertificates('default')).removed.length});`;
            const dataUrl = await spawn(
                new URL('data:text/javascript,' + encodeURIComponent(dataUrlScript)),
                { workerData: { root, policyUrl: pathToFileURL(path.join(root, 'runtime-policy.cjs')).href } }
            );

            console.log(JSON.stringify({ evalCjs, evalEsm, cjsFile, esmFile, fileUrl, dataUrl }));
            """
            result, _ = self.run_node(script, args=[str(cjs_worker), str(esm_worker)], is_esm=True)
            for key in ("evalCjs", "evalEsm", "cjsFile", "esmFile", "fileUrl", "dataUrl"):
                self.assertEqual(result[key]["blocked"], 0, key)
                self.assertTrue(result[key]["instanceofWorker"], key)

    def test_preload_order_before_user_preload_and_option_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_preload = Path(tmp) / "user-preload.cjs"
            user_preload.write_text(
                "'use strict';\n"
                "const tls = require('node:tls');\n"
                "const path = require('node:path');\n"
                "const policy = require(path.join(process.env.TEST_SECURITY_ROOT, 'runtime-policy.cjs'));\n"
                "globalThis.__USER_PRELOAD_OBSERVED_BLOCKED__ = policy.filterRoots(tls.getCACertificates('default')).removed.length;\n"
                "globalThis.__USER_PRELOAD_RAN__ = true;\n",
                encoding="utf-8",
            )
            script = r"""
            const path = require('node:path');
            const root = process.argv[2];
            const userPreload = process.argv[3];
            require(path.join(root, 'worker-guard.cjs')).installWorkerGuard();
            const { Worker, SHARE_ENV } = require('node:worker_threads');

            function runWithOptions(options) {
                return new Promise((resolve, reject) => {
                    const code = `
                        const tls = require('node:tls');
                        const path = require('node:path');
                        const { parentPort, workerData } = require('node:worker_threads');
                        const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));
                        parentPort.postMessage({
                            blockedAtEntry: policy.filterRoots(tls.getCACertificates('default')).removed.length,
                            userPreloadRan: !!globalThis.__USER_PRELOAD_RAN__,
                            userPreloadObservedBlocked: globalThis.__USER_PRELOAD_OBSERVED_BLOCKED__ ?? null,
                            customEnv: process.env.CUSTOM_WORKER_FLAG || null,
                            argv: process.argv.slice(2),
                            execArgv: process.execArgv,
                        });
                    `;
                    const w = new Worker(code, { eval: true, workerData: { root }, ...options });
                    w.on('message', resolve);
                    w.on('error', reject);
                });
            }

            (async () => {
                // 1. Explicit empty execArgv: [] + custom env object + argv
                const emptyExecArgv = await runWithOptions({
                    execArgv: [],
                    env: { ...process.env, CUSTOM_WORKER_FLAG: 'custom_ok' },
                    argv: ['arg1', 'arg2'],
                });

                // 2. Caller supplies its own --require userPreload: our security preload must run BEFORE it!
                const withUserPreload = await runWithOptions({
                    execArgv: ['--require', userPreload],
                    env: { ...process.env, TEST_SECURITY_ROOT: root, CUSTOM_WORKER_FLAG: 'preload_order' },
                });

                // 3. SHARE_ENV option
                process.env.SHARED_ENV_PROBE = 'shared_ok';
                const withShareEnv = await runWithOptions({
                    env: SHARE_ENV,
                });

                // 4. Parent process.execArgv has both legitimate '--require' userPreload AND
                // Electron-like '--no-sandbox' when options.execArgv is omitted:
                // '--no-sandbox' must be filtered out while parent '--require' userPreload MUST be preserved
                // and executed AFTER our security preload!
                const origExecArgv = process.execArgv.slice();
                process.env.TEST_SECURITY_ROOT = root;
                process.execArgv = ['--no-sandbox', '--require', userPreload];
                let withHostFlagAndParentPreload;
                try {
                    withHostFlagAndParentPreload = await runWithOptions({});
                } finally {
                    process.execArgv = origExecArgv;
                    delete process.env.TEST_SECURITY_ROOT;
                }

                // 5. Unknown invalid flag in parent process.execArgv MUST fail closed (not silently cleared)
                process.execArgv = ['--title=invalid-worker-flag', '--require', userPreload];
                let unknownParentFlagFailedClosed = false;
                try {
                    await runWithOptions({});
                } catch (e) {
                    unknownParentFlagFailedClosed = e && e.code === 'ERR_WORKER_INVALID_EXEC_ARGV';
                } finally {
                    process.execArgv = origExecArgv;
                }

                console.log(JSON.stringify({
                    emptyExecArgv,
                    withUserPreload,
                    withShareEnv,
                    withHostFlagAndParentPreload,
                    unknownParentFlagFailedClosed,
                }));
            })();
            """
            result, _ = self.run_node(script, args=[str(user_preload)])
            self.assertEqual(result["emptyExecArgv"]["blockedAtEntry"], 0)
            self.assertEqual(result["emptyExecArgv"]["customEnv"], "custom_ok")
            self.assertEqual(result["emptyExecArgv"]["argv"], ["arg1", "arg2"])

            self.assertTrue(result["withUserPreload"]["userPreloadRan"])
            self.assertEqual(result["withUserPreload"]["userPreloadObservedBlocked"], 0)
            self.assertEqual(result["withUserPreload"]["blockedAtEntry"], 0)
            self.assertEqual(
                Path(result["withUserPreload"]["execArgv"][1]).resolve(),
                (ROOT / "node-preload.cjs").resolve(),
            )

            self.assertEqual(result["withShareEnv"]["blockedAtEntry"], 0)
            self.assertEqual(result["withHostFlagAndParentPreload"]["blockedAtEntry"], 0)
            self.assertTrue(result["withHostFlagAndParentPreload"]["userPreloadRan"])
            self.assertEqual(result["withHostFlagAndParentPreload"]["userPreloadObservedBlocked"], 0)
            self.assertNotIn("--no-sandbox", result["withHostFlagAndParentPreload"]["execArgv"])
            self.assertTrue(result["unknownParentFlagFailedClosed"])

    def test_recursive_nested_workers(self):
        script = r"""
        const path = require('node:path');
        const root = process.argv[2];
        require(path.join(root, 'worker-guard.cjs')).installWorkerGuard();
        const { Worker } = require('node:worker_threads');

        const level2Code = `
            const tls = require('node:tls');
            const path = require('node:path');
            const { parentPort, workerData, threadId } = require('node:worker_threads');
            const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));
            parentPort.postMessage({
                level: 2,
                threadId,
                blocked: policy.filterRoots(tls.getCACertificates('default')).removed.length,
            });
        `;

        const level1Code = `
            const tls = require('node:tls');
            const path = require('node:path');
            const { Worker, parentPort, workerData, threadId } = require('node:worker_threads');
            const policy = require(path.join(workerData.root, 'runtime-policy.cjs'));
            const level1Blocked = policy.filterRoots(tls.getCACertificates('default')).removed.length;
            // Spawn nested worker WITHOUT explicit preload options
            const child = new Worker(workerData.level2Code, {
                eval: true,
                workerData: { root: workerData.root },
            });
            child.on('message', childMsg => {
                parentPort.postMessage({
                    level: 1,
                    threadId,
                    blocked: level1Blocked,
                    child: childMsg,
                });
            });
            child.on('error', err => { throw err; });
        `;

        const w = new Worker(level1Code, {
            eval: true,
            workerData: { root, level2Code },
        });
        w.on('message', msg => console.log(JSON.stringify(msg)));
        w.on('error', err => { throw err; });
        """
        result, _ = self.run_node(script)
        self.assertEqual(result["level"], 1)
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["child"]["level"], 2)
        self.assertEqual(result["child"]["blocked"], 0)
        self.assertNotEqual(result["threadId"], result["child"]["threadId"])

    def test_bootstrap_mjs_initializes_worker_guard(self):
        script = r"""
        import path from 'node:path';
        import { pathToFileURL } from 'node:url';
        const root = process.argv[2];
        await import(pathToFileURL(path.join(root, 'bootstrap.mjs')).href);
        const { Worker } = await import('node:worker_threads');

        const msg = await new Promise((resolve, reject) => {
            const w = new Worker(
                `const tls=require('node:tls'),path=require('node:path'),{parentPort,workerData}=require('node:worker_threads');` +
                `const policy=require(path.join(workerData.root,'runtime-policy.cjs'));` +
                `parentPort.postMessage({blocked:policy.filterRoots(tls.getCACertificates('default')).removed.length});`,
                { eval: true, workerData: { root } }
            );
            w.on('message', resolve);
            w.on('error', reject);
        });
        console.log(JSON.stringify(msg));
        """
        result, stderr = self.run_node(script, is_esm=True)
        self.assertEqual(result["blocked"], 0)
        events = [json.loads(line) for line in stderr.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "policy-ready")
        self.assertTrue(events[0]["worker_guard"])

    def test_fail_closed_on_missing_apis_or_unsafe_options(self):
        script = r"""
        const path = require('node:path');
        const root = process.argv[2];
        const guard = require(path.join(root, 'worker-guard.cjs'));

        function throwsWith(fn, pattern) {
            try {
                fn();
                return false;
            } catch (e) {
                return pattern.test(String(e.message || e));
            }
        }

        const missingGetCA = throwsWith(
            () => guard.installWorkerGuard({ tls: { setDefaultCACertificates() {} } }),
            /Required Node CA APIs unavailable/
        );
        const missingSetCA = throwsWith(
            () => guard.installWorkerGuard({ tls: { getCACertificates() { return []; } } }),
            /Required Node CA APIs unavailable/
        );
        const missingSync = throwsWith(
            () => guard.installWorkerGuard({ module: {} }),
            /syncBuiltinESMExports unavailable/
        );
        const missingPreload = throwsWith(
            () => guard.installWorkerGuard({ preloadPath: path.join(root, 'nonexistent-preload.cjs') }),
            /Required worker preload missing/
        );
        const unsafeEnvInstall = throwsWith(
            () => guard.installWorkerGuard({ env: { NODE_TLS_REJECT_UNAUTHORIZED: '0' } }),
            /NODE_TLS_REJECT_UNAUTHORIZED=0/
        );
        const unsafeNodeOptionsInstall = throwsWith(
            () => guard.installWorkerGuard({ env: { NODE_OPTIONS: '--require ./evil.cjs' } }),
            /NODE_OPTIONS/
        );
        const unsafeExtraCaInstall = throwsWith(
            () => guard.installWorkerGuard({ env: { NODE_EXTRA_CA_CERTS: '/tmp/custom.pem' } }),
            /NODE_EXTRA_CA_CERTS/
        );

        guard.installWorkerGuard();
        const { Worker } = require('node:worker_threads');
        const unsafeWorkerEnv = throwsWith(
            () => new Worker('1', { eval: true, env: { NODE_TLS_REJECT_UNAUTHORIZED: '0' } }),
            /NODE_TLS_REJECT_UNAUTHORIZED=0/
        );
        const unsafeWorkerNodeOptions = throwsWith(
            () => new Worker('1', { eval: true, env: { NODE_OPTIONS: '--require ./evil.cjs' } }),
            /NODE_OPTIONS/
        );
        const unsafeWorkerExtraCa = throwsWith(
            () => new Worker('1', { eval: true, env: { NODE_EXTRA_CA_CERTS: '/tmp/custom.pem' } }),
            /NODE_EXTRA_CA_CERTS/
        );
        process.env.NODE_OPTIONS = '--no-warnings';
        let unsafeParentNodeOptionsAtSpawn = false;
        try {
            unsafeParentNodeOptionsAtSpawn = throwsWith(
                () => new Worker('1', { eval: true }),
                /NODE_OPTIONS/
            );
        } finally {
            delete process.env.NODE_OPTIONS;
        }

        // Real 3rd-argument bypass regression: passing any 3rd argument to new Worker(fn, opts, thirdArg)
        // MUST fail closed instead of calling super(fn, opts, thirdArg) without preload!
        const thirdArgBypassRejected = throwsWith(
            () => new Worker('const {parentPort}=require("node:worker_threads");parentPort.postMessage("escaped");', { eval: true }, 'bypass-attempt'),
            /Unsupported extra arguments/
        );
        const thirdArgSymbolRejected = throwsWith(
            () => new Worker('1', { eval: true }, Symbol('kIsInternalFake')),
            /Unsupported extra arguments/
        );
        const invalidExecArgv = throwsWith(
            () => new Worker('1', { eval: true, execArgv: '--not-an-array' }),
            /options\.execArgv/
        );
        const invalidOptions = throwsWith(
            () => new Worker('1', 'not-an-object'),
            /options/
        );

        // Verify mock TLS store with all 4 affected roots triggers full subtraction
        // even on a host runtime where not all 4 roots are present in bundled defaults.
        const evidence = require(path.join(root, 'ca-evidence.json'));
        const tls = require('node:tls');
        const safePem = tls.rootCertificates.find(x => !require(path.join(root, 'runtime-policy.cjs')).inspectChain({ data: x }).blocked);
        let mockStore = [safePem, ...evidence.roots.map(r => r.pem)];
        const mockTls = {
            getCACertificates: () => mockStore,
            setDefaultCACertificates: (next) => { mockStore = Array.from(next); },
        };
        const mockWt = { Worker: class MockWorker {}, threadId: 99, isMainThread: false };
        const mockMod = { syncBuiltinESMExports() { this.synced = true; } };
        guard.installWorkerGuard({ tls: mockTls, workerThreads: mockWt, module: mockMod });
        const mockStoreAll4Removed = mockStore.length === 1 && mockStore[0] === safePem && mockMod.synced === true;

        console.log(JSON.stringify({
            missingGetCA,
            missingSetCA,
            missingSync,
            missingPreload,
            unsafeEnvInstall,
            unsafeNodeOptionsInstall,
            unsafeExtraCaInstall,
            unsafeWorkerEnv,
            unsafeWorkerNodeOptions,
            unsafeWorkerExtraCa,
            unsafeParentNodeOptionsAtSpawn,
            thirdArgBypassRejected,
            thirdArgSymbolRejected,
            invalidExecArgv,
            invalidOptions,
            mockStoreAll4Removed,
        }));
        """
        result, _ = self.run_node(script)
        for k, v in result.items():
            self.assertTrue(v, k)


if __name__ == "__main__":
    unittest.main()
