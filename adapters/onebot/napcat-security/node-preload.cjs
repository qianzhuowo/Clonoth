'use strict';
// Worker thread preload: installs subtract-only Node CA policy AND recursive
// Worker guard before any user preload or worker entry module runs.
// No circular execution: worker-guard.cjs only references this file's path
// via --require and never requires './node-preload.cjs' directly.
require('./runtime-policy.cjs').installNode();
require('./worker-guard.cjs').installWorkerGuard();
