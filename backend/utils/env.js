/**
 * @file   .env loader
 * @module utils/env
 *
 * Loads the project's `.env` file into `process.env` before any other module
 * reads configuration. Node 20 ships `process.loadEnvFile`, which does not
 * overwrite variables that are already set, so a deployment that injects its
 * secrets through the environment keeps winning over a stale local file.
 *
 * @author Eltokhey
 * @since  2026-10
 */
const fs = require('fs');
const path = require('path');

const ENV_PATH = path.resolve(__dirname, '..', '..', '.env');

let loaded = false;

/**
 * Minimal fallback parser for runtimes without `process.loadEnvFile`.
 *
 * Supports the subset the project actually uses: `KEY=value`, optional single
 * or double quotes, `#` comments and blank lines. It deliberately does not try
 * to implement shell interpolation.
 *
 * @param {string} file
 * @returns {void}
 */
function parseEnvFile(file) {
  const text = fs.readFileSync(file, 'utf8');
  text.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return;
    const eq = trimmed.indexOf('=');
    if (eq < 1) return;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  });
}

/**
 * Loads `.env` once per process. A missing file is normal — production supplies
 * its variables directly — so it is silent rather than fatal.
 *
 * @returns {boolean} whether a file was loaded
 */
function loadEnv() {
  if (loaded) return true;
  loaded = true;

  try {
    if (!fs.existsSync(ENV_PATH)) return false;
    if (typeof process.loadEnvFile === 'function') {
      process.loadEnvFile(ENV_PATH);
    } else {
      parseEnvFile(ENV_PATH);
    }
    return true;
  } catch (err) {
    // A malformed or unreadable file must not stop the process from booting on
    // variables that were injected another way.
    console.warn('[env] Could not load .env:', err.message);
    return false;
  }
}

module.exports = loadEnv;
