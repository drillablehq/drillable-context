#!/usr/bin/env node
'use strict';
//
// drillable-context — npx launcher for the bundled, stdlib-only Python MCP server.
//
// `npx drillable-context <args>` is just `python3 src/server.py <args>`: every argument is forwarded
// verbatim and stdio is INHERITED, so the MCP stdio JSON-RPC stream is byte-for-byte transparent —
// this wrapper never writes to stdout itself (only ever to stderr, on a launch error). It pulls in no
// npm runtime dependencies and installs no Python packages; the server stays stdlib-only.
//
// Pick a specific interpreter with $DRILLABLE_PYTHON (otherwise: python3, then python — must be 3.x).
//
const { spawn, spawnSync } = require('child_process');
const path = require('path');

function resolvePython() {
  const candidates = process.env.DRILLABLE_PYTHON ? [process.env.DRILLABLE_PYTHON] : ['python3', 'python'];
  for (const cmd of candidates) {
    const probe = spawnSync(cmd, ['-c', 'import sys; print(sys.version_info[0])'], { encoding: 'utf8' });
    if (!probe.error && probe.status === 0 && probe.stdout.trim().startsWith('3')) {
      return cmd;
    }
  }
  return null;
}

const python = resolvePython();
if (!python) {
  process.stderr.write(
    'drillable-context: Python 3 not found on PATH. Install Python 3 (the server is stdlib-only — no '
    + 'pip needed), or point DRILLABLE_PYTHON at your interpreter.\n');
  process.exit(127);
}

const server = path.join(__dirname, '..', 'src', 'server.py');
const child = spawn(python, [server, ...process.argv.slice(2)], { stdio: 'inherit' });

// Forward termination so the Python server never outlives this launcher.
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => { try { child.kill(sig); } catch (_) { /* already gone */ } });
}

child.on('error', (err) => {
  process.stderr.write(`drillable-context: failed to launch ${python}: ${err.message}\n`);
  process.exit(127);
});
child.on('exit', (code, signal) => {
  process.exitCode = signal ? 1 : (code == null ? 0 : code);
});
