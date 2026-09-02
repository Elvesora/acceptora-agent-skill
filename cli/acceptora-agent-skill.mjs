#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { createHash, randomUUID } from 'node:crypto';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_DOCUMENT = JSON.parse(readFileSync(join(PACKAGE_ROOT, 'package.json'), 'utf8'));
const ACCEPTORA_ORIGIN = 'https://www.acceptora.com';
const PROJECT_URL = `${ACCEPTORA_ORIGIN}/api/v1/integrations/project`;
const CONNECTION_CONFIRMATION_URL = `${ACCEPTORA_ORIGIN}/api/v1/integrations/connection/confirm`;
const CONFIG_PATH = '.acceptora/config.json';
const INSTALL_MANIFEST_PATH = '.acceptora/install-manifest.json';
const PROJECT_ENV_PATH = '.acceptora-env';
const PROJECT_TOKEN_ENV = 'ACCEPTORA_PROJECT_TOKEN';
const TOKEN_PATTERN = /^avt_(?<ulid>[0-9A-HJKMNP-TV-Z]{26})_[A-Za-z0-9]{48}$/;
const LEGACY_TOKEN_ENV_PATTERN = /^ACCEPTORA_AGENT_TOKEN_PROJ_[0-9A-HJKMNP-TV-Z]{26}$/;
const PROJECT_ID_PATTERN = /^proj_[0-9A-HJKMNP-TV-Z]{26}$/;
const VERSION_PATTERN = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const REQUIRED_SCOPES = new Set([
  'projects:read',
  'features:resolve',
  'features:read',
  'checklists:write',
  'feedback:read',
  'feedback:address',
  'gates:read',
]);
const CLIENTS = {
  codex: {
    label: 'Codex',
    skillDirectory: '.agents/skills/acceptora',
    legacyInstructionFile: 'AGENTS.md',
    mcpFile: '.codex/config.toml',
  },
  'claude-code': {
    label: 'Claude Code',
    skillDirectory: '.claude/skills/acceptora',
    legacyInstructionFile: 'CLAUDE.md',
    mcpFile: '.mcp.json',
  },
  'gemini-cli': {
    label: 'Gemini CLI',
    skillDirectory: '.gemini/skills/acceptora',
    legacyInstructionFile: 'GEMINI.md',
    mcpFile: '.gemini/settings.json',
  },
};
const SKILL_PAYLOAD = [
  'SKILL.md',
  'agents/openai.yaml',
  'references/api-mcp.md',
  'scripts/mcp-headers.mjs',
  'scripts/project_context.py',
];
const INSTRUCTION_START = '<!-- acceptora:start -->';
const INSTRUCTION_END = '<!-- acceptora:end -->';
const MCP_START = '# acceptora-mcp:start';
const MCP_END = '# acceptora-mcp:end';
const MAX_RESPONSE_BYTES = 1_048_576;

export class CliError extends Error {}

class CancelledError extends Error {}

function writeLine(stream, message = '') {
  stream.write(`${message}\n`);
}

function runtimeWith(overrides) {
  return {
    cwd: process.cwd(),
    env: process.env,
    platform: process.platform,
    stdin: process.stdin,
    stdout: process.stdout,
    stderr: process.stderr,
    fetch: globalThis.fetch,
    spawnSync,
    readSecret: null,
    atomicWrite: null,
    ...overrides,
  };
}

function parseArguments(argv) {
  const values = [...argv];
  const command = values.shift();
  if (command === undefined || command === '--help' || command === '-h') {
    return { command: 'help' };
  }
  if (!['install', 'update', 'doctor', 'uninstall'].includes(command)) {
    throw new CliError('Unknown command. Run with --help to see supported commands.');
  }

  const options = { command, client: null, projectRoot: null };
  while (values.length > 0) {
    const flag = values.shift();
    if (!['--client', '--project-root'].includes(flag)) {
      throw new CliError('Unknown option. Run with --help to see supported options.');
    }
    const value = values.shift();
    if (value === undefined || value.startsWith('--')) {
      throw new CliError(`${flag} requires a value.`);
    }
    if (flag === '--client') {
      options.client = value;
    } else if (flag === '--project-root') {
      options.projectRoot = value;
    }
  }
  if (options.client !== null && CLIENTS[options.client] === undefined) {
    throw new CliError('Unsupported coding client.');
  }
  return options;
}

function printHelp(runtime) {
  writeLine(runtime.stdout, 'Acceptora Agent Skill');
  writeLine(runtime.stdout);
  writeLine(runtime.stdout, 'Usage:');
  writeLine(runtime.stdout, '  acceptora-agent-skill install --client <codex|claude-code|gemini-cli>');
  writeLine(runtime.stdout, '  acceptora-agent-skill update [--client <client>]');
  writeLine(runtime.stdout, '  acceptora-agent-skill doctor [--client <client>]');
  writeLine(runtime.stdout, '  acceptora-agent-skill uninstall [--client <client>]');
}

function secretFreeEnvironment(runtime) {
  return Object.fromEntries(Object.entries(runtime.env).filter(([name]) => {
    const normalized = name.toUpperCase();
    return normalized !== PROJECT_TOKEN_ENV
      && normalized !== 'ACCEPTORA_AGENT_TOKEN'
      && !normalized.startsWith('ACCEPTORA_AGENT_TOKEN_');
  }));
}

function runGit(runtime, root, arguments_, allowed = [0]) {
  const environment = secretFreeEnvironment(runtime);
  for (const name of Object.keys(environment)) {
    const normalized = name.toUpperCase();
    if (normalized.startsWith('GIT_') || normalized === 'SSH_ASKPASS' || normalized === 'SSH_ASKPASS_REQUIRE') {
      delete environment[name];
    }
  }
  Object.assign(environment, {
    GIT_ASKPASS: '',
    SSH_ASKPASS: '',
    GIT_CONFIG_GLOBAL: runtime.platform === 'win32' ? 'NUL' : '/dev/null',
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_TERMINAL_PROMPT: '0',
    GCM_INTERACTIVE: 'Never',
  });
  const result = runtime.spawnSync('git', ['-C', root, ...arguments_], {
    encoding: 'utf8',
    env: environment,
    input: '',
    timeout: 5_000,
    windowsHide: true,
  });
  if (result.error || !allowed.includes(result.status)) {
    const message = String(result.stderr ?? '').trim();
    throw new CliError(message || 'Git inspection failed.');
  }
  return result;
}

function resolveProjectRoot(runtime, requestedRoot) {
  const selected = resolve(requestedRoot ?? runtime.cwd);
  if (!existsSync(selected) || !lstatSync(selected).isDirectory()) {
    throw new CliError('The selected project directory does not exist.');
  }
  const output = runGit(runtime, selected, ['rev-parse', '--show-toplevel']).stdout.trim();
  if (!output) {
    throw new CliError('Git returned an invalid project root.');
  }
  return realpathSync(output);
}

function safeProjectPath(root, relativePath) {
  if (!relativePath || isAbsolute(relativePath)) {
    throw new CliError(`Unsafe project path: ${relativePath}`);
  }
  const parts = relativePath.replaceAll('\\', '/').split('/');
  if (parts.some((part) => !part || part === '.' || part === '..')) {
    throw new CliError(`Unsafe project path: ${relativePath}`);
  }
  const destination = resolve(root, ...parts);
  if (destination !== root && !destination.startsWith(`${root}${sep}`)) {
    throw new CliError(`Project path escapes the worktree: ${relativePath}`);
  }
  let cursor = root;
  for (const part of parts) {
    cursor = join(cursor, part);
    if (existsSync(cursor) && lstatSync(cursor).isSymbolicLink()) {
      throw new CliError(`Project path crosses a link: ${relativePath}`);
    }
  }
  return destination;
}

function readText(path, label) {
  if (!existsSync(path)) {
    return '';
  }
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new CliError(`${label} is not a regular file.`);
  }
  return readFileSync(path, 'utf8');
}

function atomicWrite(path, content) {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = join(dirname(path), `.${basename(path)}.acceptora-${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { encoding: 'utf8', flag: 'wx' });
    renameSync(temporary, path);
  } finally {
    if (existsSync(temporary)) {
      unlinkSync(temporary);
    }
  }
}

function writeProjectFile(runtime, path, content) {
  if (runtime.atomicWrite !== null) {
    runtime.atomicWrite(path, content, atomicWrite);
    return;
  }
  atomicWrite(path, content);
}

function snapshotFile(path) {
  return existsSync(path)
    ? { path, existed: true, content: readFileSync(path) }
    : { path, existed: false, content: null };
}

function restoreFiles(snapshots) {
  for (const snapshot of [...snapshots].reverse()) {
    if (snapshot.existed) {
      atomicWrite(snapshot.path, snapshot.content);
    } else if (existsSync(snapshot.path)) {
      unlinkSync(snapshot.path);
    }
  }
}

function countOccurrences(value, needle) {
  return value.split(needle).length - 1;
}

function managedBounds(value, startMarker, endMarker, label) {
  const starts = countOccurrences(value, startMarker);
  const ends = countOccurrences(value, endMarker);
  if (starts === 0 && ends === 0) {
    return null;
  }
  if (starts !== 1 || ends !== 1) {
    throw new CliError(`${label} contains ambiguous Acceptora markers.`);
  }
  const start = value.indexOf(startMarker);
  const end = value.indexOf(endMarker, start) + endMarker.length;
  if (end <= start) {
    throw new CliError(`${label} contains invalid Acceptora markers.`);
  }
  return [start, end];
}

function upsertManagedBlock(value, block, startMarker, endMarker, label) {
  const bounds = managedBounds(value, startMarker, endMarker, label);
  if (bounds !== null) {
    return value.slice(0, bounds[0]) + block + value.slice(bounds[1]);
  }
  const separator = value && !value.endsWith('\n') ? '\n' : '';
  return `${value}${separator}${block}\n`;
}

function removeManagedBlock(value, startMarker, endMarker, label) {
  const bounds = managedBounds(value, startMarker, endMarker, label);
  if (bounds === null) {
    throw new CliError(`${label} is missing its installer-owned Acceptora block.`);
  }
  let before = value.slice(0, bounds[0]);
  let after = value.slice(bounds[1]);
  if (before.endsWith('\n') && after.startsWith('\n')) {
    after = after.slice(1);
  }
  return before + after;
}

function validateCredentialFormat(token) {
  if (!TOKEN_PATTERN.test(token)) {
    throw new CliError('The supplied value is not a valid Acceptora project key.');
  }
}

function projectIdentity(projectId) {
  if (typeof projectId !== 'string' || !PROJECT_ID_PATTERN.test(projectId)) {
    throw new CliError('Acceptora returned an invalid project identity.');
  }
  return {
    projectId,
    tokenEnv: PROJECT_TOKEN_ENV,
  };
}

async function fetchJson(runtime, url, options, failureMessage) {
  let response;
  try {
    response = await runtime.fetch(url, {
      ...options,
      redirect: 'error',
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    throw new CliError(failureMessage);
  }
  if (!response.ok) {
    throw new CliError(url === PROJECT_URL && (response.status === 401 || response.status === 403)
      ? 'Acceptora rejected the supplied project key.'
      : failureMessage);
  }
  const declaredLength = Number.parseInt(response.headers.get('content-length') ?? '', 10);
  if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
    throw new CliError(failureMessage);
  }
  const chunks = [];
  let received = 0;
  const reader = response.body?.getReader();
  if (reader === undefined) {
    throw new CliError(failureMessage);
  }
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    received += value.byteLength;
    if (received > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new CliError(failureMessage);
    }
    chunks.push(Buffer.from(value));
  }
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks, received));
  } catch {
    throw new CliError(failureMessage);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new CliError(failureMessage);
  }
}

async function validateProjectKey(runtime, token) {
  validateCredentialFormat(token);
  const payload = await fetchJson(runtime, PROJECT_URL, {
    method: 'GET',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'User-Agent': `Acceptora-Agent-Skill/${PACKAGE_DOCUMENT.version}`,
    },
  }, 'Acceptora project verification failed.');
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new CliError('Acceptora returned an invalid project response.');
  }
  const identity = projectIdentity(payload.project_id);
  const scopes = payload.granted_scopes;
  if (!Array.isArray(scopes)
    || scopes.length > 64
    || new Set(scopes).size !== scopes.length
    || scopes.some((scope) => typeof scope !== 'string' || scope.length === 0 || scope.length > 128)) {
    throw new CliError('Acceptora returned invalid project scopes.');
  }
  if ([...REQUIRED_SCOPES].some((scope) => !scopes.includes(scope))) {
    throw new CliError('The project key lacks required Acceptora workflow scopes.');
  }
  return identity;
}

async function confirmConnection(runtime, token) {
  await fetchJson(runtime, CONNECTION_CONFIRMATION_URL, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'User-Agent': `Acceptora-Agent-Skill/${PACKAGE_DOCUMENT.version}`,
    },
    body: '{}',
  }, 'Acceptora connection confirmation failed. Local setup is complete; run update to retry confirmation. Do not run install again.');
}

async function hiddenPrompt(runtime) {
  if (runtime.readSecret !== null) {
    return runtime.readSecret();
  }
  const { stdin, stdout } = runtime;
  if (!stdin.isTTY || !stdout.isTTY || typeof stdin.setRawMode !== 'function') {
    throw new CliError('Run the installer in an interactive terminal so the project key can be entered securely.');
  }
  stdout.write('Acceptora project key (hidden): ');
  return new Promise((resolvePromise, rejectPromise) => {
    let value = '';
    const previousRawMode = stdin.isRaw;
    const cleanup = () => {
      stdin.off('data', onData);
      stdin.setRawMode(previousRawMode ?? false);
      stdin.pause();
      stdout.write('\n');
    };
    const onData = (chunk) => {
      for (const character of String(chunk)) {
        if (character === '\u0003') {
          cleanup();
          rejectPromise(new CancelledError());
          return;
        }
        if (character === '\r' || character === '\n') {
          cleanup();
          resolvePromise(value);
          return;
        }
        if (character === '\u0008' || character === '\u007f') {
          if (value.length > 0) {
            value = value.slice(0, -1);
            stdout.write('\b \b');
          }
        } else if (value.length < 255) {
          value += character;
          stdout.write('*');
        }
      }
    };
    stdin.setEncoding('utf8');
    stdin.setRawMode(true);
    stdin.resume();
    stdin.on('data', onData);
  });
}

function projectCredentialFile(runtime, root) {
  const path = safeProjectPath(root, PROJECT_ENV_PATH);
  const tracked = runGit(runtime, root, ['ls-files', '--error-unmatch', '--', PROJECT_ENV_PATH], [0, 1]).status === 0;
  if (tracked) {
    throw new CliError(`${PROJECT_ENV_PATH} is tracked by Git. Remove it from version control before storing a project key.`);
  }
  const current = readText(path, 'Acceptora project environment file');
  const lines = current.split(/\r\n|\n|\r/);
  const assignments = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim() || line.trimStart().startsWith('#')) {
      continue;
    }
    if (!line.startsWith(`${PROJECT_TOKEN_ENV}=`)) {
      throw new CliError(`${PROJECT_ENV_PATH} contains an unsupported entry.`);
    }
    assignments.push({ index, token: line.slice(PROJECT_TOKEN_ENV.length + 1) });
  }
  if (assignments.length > 1) {
    throw new CliError(`${PROJECT_ENV_PATH} contains duplicate ${PROJECT_TOKEN_ENV} entries.`);
  }
  return {
    path,
    current,
    token: assignments[0]?.token ?? null,
  };
}

function projectCredentialContent(current, token) {
  const assignment = `${PROJECT_TOKEN_ENV}=${token}`;
  if (!current) {
    return `${assignment}\n`;
  }
  const expression = new RegExp(`^${PROJECT_TOKEN_ENV}=.*$`, 'm');
  if (expression.test(current)) {
    return current.replace(expression, assignment);
  }
  const newline = current.includes('\r\n') ? '\r\n' : '\n';
  const separator = current.endsWith('\n') || current.endsWith('\r') ? '' : newline;
  return `${current}${separator}${assignment}${newline}`;
}

async function installCredential(runtime, root) {
  const file = projectCredentialFile(runtime, root);
  const token = file.token ?? await hiddenPrompt(runtime);
  return { file, token, content: projectCredentialContent(file.current, token) };
}

async function updateCredential(runtime, root, config) {
  const file = projectCredentialFile(runtime, root);
  const legacyToken = LEGACY_TOKEN_ENV_PATTERN.test(config.token_env) ? runtime.env[config.token_env] : null;
  const token = file.token ?? legacyToken ?? await hiddenPrompt(runtime);
  return { file, token, content: projectCredentialContent(file.current, token) };
}

function doctorCredential(runtime, root, config) {
  const file = projectCredentialFile(runtime, root);
  const legacyToken = LEGACY_TOKEN_ENV_PATTERN.test(config.token_env) ? runtime.env[config.token_env] : null;
  const token = file.token ?? legacyToken;
  if (!token) {
    throw new CliError(`${PROJECT_ENV_PATH} does not contain ${PROJECT_TOKEN_ENV}. Run update to add the project key.`);
  }
  return token;
}

function expectedPayload() {
  return new Map(SKILL_PAYLOAD.map((relativePath) => {
    const path = resolve(PACKAGE_ROOT, ...relativePath.split('/'));
    if (!existsSync(path) || !lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()) {
      throw new CliError(`Package payload is unavailable: ${relativePath}`);
    }
    return [relativePath, readFileSync(path)];
  }));
}

function sha256(body) {
  return `sha256:${createHash('sha256').update(body).digest('hex')}`;
}

function installManifestDocument(client, payload, createdFiles) {
  return {
    schema_version: 1,
    client,
    package_version: PACKAGE_DOCUMENT.version,
    instruction: '',
    created_files: {
      instruction: false,
      mcp: createdFiles.mcp,
    },
    payload: Object.fromEntries([...payload].map(([relativePath, body]) => [relativePath, sha256(body)])),
  };
}

function loadInstallManifest(root) {
  const path = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  const document = parseJsonDocument(readText(path, 'Acceptora install manifest'), 'Acceptora install manifest');
  const keys = Object.keys(document).sort();
  if (!isDeepStrictEqual(keys, ['client', 'created_files', 'instruction', 'package_version', 'payload', 'schema_version'])
    || document.schema_version !== 1
    || CLIENTS[document.client] === undefined
    || typeof document.instruction !== 'string'
    || (document.instruction !== ''
      && (!document.instruction.startsWith(INSTRUCTION_START)
        || !document.instruction.endsWith(INSTRUCTION_END)))
    || typeof document.package_version !== 'string'
    || !VERSION_PATTERN.test(document.package_version)
    || document.created_files === null
    || typeof document.created_files !== 'object'
    || Array.isArray(document.created_files)
    || !isDeepStrictEqual(Object.keys(document.created_files).sort(), ['instruction', 'mcp'])
    || typeof document.created_files.instruction !== 'boolean'
    || typeof document.created_files.mcp !== 'boolean'
    || document.payload === null
    || typeof document.payload !== 'object'
    || Array.isArray(document.payload)) {
    throw new CliError('Acceptora install manifest is invalid.');
  }
  const paths = Object.keys(document.payload).sort();
  if (paths.length === 0 || paths.some((relativePath) => {
    try {
      safeProjectPath(root, relativePath);
      return !DIGEST_PATTERN.test(document.payload[relativePath]);
    } catch {
      return true;
    }
  })) {
    throw new CliError('Acceptora install manifest is invalid.');
  }
  return document;
}

function prepareLegacyInstructionRemoval(root, profile, installManifest) {
  if (installManifest.instruction === '') {
    return null;
  }
  const path = safeProjectPath(root, profile.legacyInstructionFile);
  const current = readText(path, 'Project instruction file');
  if (countOccurrences(current, installManifest.instruction) !== 1) {
    return null;
  }
  const start = current.indexOf(installManifest.instruction);
  let before = current.slice(0, start);
  let after = current.slice(start + installManifest.instruction.length);
  if (before.endsWith('\n') && after.startsWith('\n')) {
    after = after.slice(1);
  }
  const content = before + after;
  return {
    path,
    content,
    remove: installManifest.created_files.instruction && content.trim() === '',
  };
}

function applyLegacyInstructionRemoval(runtime, mutation) {
  if (mutation === null) {
    return;
  }
  if (mutation.remove) {
    unlinkSync(mutation.path);
    return;
  }
  writeProjectFile(runtime, mutation.path, mutation.content);
}

function listSkillFiles(root, directory = root, result = []) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    const metadata = lstatSync(path);
    if (metadata.isSymbolicLink()) {
      throw new CliError('The installed skill contains a link.');
    }
    if (metadata.isDirectory()) {
      listSkillFiles(root, path, result);
    } else if (metadata.isFile()) {
      result.push(relative(root, path).split(sep).join('/'));
    } else {
      throw new CliError('The installed skill contains a special file.');
    }
  }
  return result.sort();
}

function requireExactSkillPaths(skillRoot, payload) {
  if (!existsSync(skillRoot) || !lstatSync(skillRoot).isDirectory() || lstatSync(skillRoot).isSymbolicLink()) {
    throw new CliError('The selected client skill is not installed safely.');
  }
  const expected = [...payload.keys()].sort();
  const observed = listSkillFiles(skillRoot);
  if (!isDeepStrictEqual(observed, expected)) {
    throw new CliError('The installed Acceptora skill contains missing or extra files.');
  }
}

function requireOwnedSkill(skillRoot, installManifest) {
  if (!existsSync(skillRoot) || !lstatSync(skillRoot).isDirectory() || lstatSync(skillRoot).isSymbolicLink()) {
    throw new CliError('The selected client skill is not installed safely.');
  }
  const expected = Object.keys(installManifest.payload).sort();
  const observed = listSkillFiles(skillRoot);
  if (!isDeepStrictEqual(observed, expected)) {
    throw new CliError('The installed Acceptora skill contains missing or extra files.');
  }
  for (const relativePath of expected) {
    const body = readFileSync(join(skillRoot, ...relativePath.split('/')));
    if (sha256(body) !== installManifest.payload[relativePath]) {
      throw new CliError(`The installed Acceptora skill has drifted: ${relativePath}`);
    }
  }
}

function payloadMatches(skillRoot, payload) {
  try {
    requireExactSkillPaths(skillRoot, payload);
    return [...payload].every(([relativePath, body]) => isDeepStrictEqual(readFileSync(join(skillRoot, ...relativePath.split('/'))), body));
  } catch {
    return false;
  }
}

function replaceSkill(root, relativeDirectory, payload, update) {
  const destination = safeProjectPath(root, relativeDirectory);
  if (!update && existsSync(destination)) {
    throw new CliError('The selected client skill directory already exists.');
  }
  mkdirSync(dirname(destination), { recursive: true });
  const staging = `${destination}.acceptora-${randomUUID()}.tmp`;
  const backup = update ? `${destination}.acceptora-backup-${randomUUID()}.tmp` : null;
  try {
    mkdirSync(staging, { recursive: false });
    for (const [relativePath, body] of payload) {
      const target = join(staging, ...relativePath.split('/'));
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, body, { flag: 'wx' });
    }
    if (update) {
      const resolvedDestination = resolve(destination);
      if (!resolvedDestination.startsWith(`${root}${sep}`)) {
        throw new CliError('Refusing to replace a skill outside the project.');
      }
      renameSync(destination, backup);
    }
    try {
      renameSync(staging, destination);
    } catch (error) {
      if (backup !== null && existsSync(backup)) {
        renameSync(backup, destination);
      }
      throw error;
    }
    return {
      commit: () => {
        if (backup !== null && existsSync(backup)) {
          rmSync(backup, { recursive: true });
        }
      },
      rollback: () => {
        if (existsSync(destination)) {
          rmSync(destination, { recursive: true });
        }
        if (backup !== null && existsSync(backup)) {
          renameSync(backup, destination);
        }
      },
    };
  } finally {
    if (existsSync(staging)) {
      rmSync(staging, { recursive: true });
    }
  }
}

function detachSkill(root, relativeDirectory) {
  const destination = safeProjectPath(root, relativeDirectory);
  const backup = `${destination}.acceptora-uninstall-${randomUUID()}.tmp`;
  renameSync(destination, backup);
  return {
    commit: () => rmSync(backup, { recursive: true }),
    rollback: () => {
      if (existsSync(backup)) {
        renameSync(backup, destination);
      }
    },
  };
}

function rollbackProjectMutation(root, skillMutation, snapshots, skillRoot) {
  let rollbackFailed = false;
  try {
    skillMutation.rollback();
  } catch {
    rollbackFailed = true;
  }
  try {
    restoreFiles(snapshots);
  } catch {
    rollbackFailed = true;
  }
  for (const path of [skillRoot, ...snapshots.map((snapshot) => snapshot.path)]) {
    try {
      removeEmptyParents(dirname(path), root);
    } catch {
      rollbackFailed = true;
    }
  }
  if (rollbackFailed) {
    throw new CliError('The operation failed and project files could not be fully restored.');
  }
}

function codexMcpBlock() {
  return `${MCP_START}\n[mcp_servers.acceptora]\nurl = "${ACCEPTORA_ORIGIN}/mcp"\nhttp_headers_helper = 'node ".agents/skills/acceptora/scripts/mcp-headers.mjs"'\n${MCP_END}`;
}

function legacyCodexMcpBlock(tokenEnv) {
  return `${MCP_START}\n[mcp_servers.acceptora]\nurl = "${ACCEPTORA_ORIGIN}/mcp"\nbearer_token_env_var = "${tokenEnv}"\n${MCP_END}`;
}

function jsonMcpServer(client, tokenEnv, legacy = false) {
  if (client === 'claude-code') {
    if (!legacy) {
      return {
        type: 'http',
        url: `${ACCEPTORA_ORIGIN}/mcp`,
        headersHelper: 'node ".claude/skills/acceptora/scripts/mcp-headers.mjs"',
      };
    }
    return {
      type: 'http',
      url: `${ACCEPTORA_ORIGIN}/mcp`,
      headers: { Authorization: `Bearer \${${tokenEnv}}` },
    };
  }
  return {
    type: 'http',
    url: `${ACCEPTORA_ORIGIN}/mcp`,
    headers: { Authorization: `Bearer \${${tokenEnv}}` },
  };
}

function parseJsonDocument(value, label) {
  if (!value.trim()) {
    return {};
  }
  let document;
  try {
    document = JSON.parse(value);
  } catch {
    throw new CliError(`${label} is invalid JSON.`);
  }
  if (document === null || typeof document !== 'object' || Array.isArray(document)) {
    throw new CliError(`${label} must contain one JSON object.`);
  }
  return document;
}

function prepareMcp(root, client, tokenEnv, owned, installedTokenEnv = tokenEnv) {
  const profile = CLIENTS[client];
  const path = safeProjectPath(root, profile.mcpFile);
  const current = readText(path, 'Project MCP config');
  if (client === 'codex') {
    const expected = codexMcpBlock();
    const bounds = managedBounds(current, MCP_START, MCP_END, 'Codex MCP config');
    if (owned) {
      const installed = installedTokenEnv === PROJECT_TOKEN_ENV
        ? codexMcpBlock()
        : legacyCodexMcpBlock(installedTokenEnv);
      if (bounds === null || current.slice(bounds[0], bounds[1]) !== installed) {
        throw new CliError('Codex MCP config installer-owned Acceptora block has drifted.');
      }
    } else if (bounds !== null || /\[mcp_servers\.acceptora\]|mcp_servers\.acceptora\.|\bacceptora\s*=/.test(current)) {
      throw new CliError('Codex MCP config already defines an unmanaged Acceptora server.');
    }
    return {
      path,
      content: upsertManagedBlock(current, expected, MCP_START, MCP_END, 'Codex MCP config'),
    };
  }

  const document = parseJsonDocument(current, 'Project MCP config');
  if (document.mcpServers === undefined) {
    document.mcpServers = {};
  }
  if (document.mcpServers === null || typeof document.mcpServers !== 'object' || Array.isArray(document.mcpServers)) {
    throw new CliError('Project MCP config mcpServers must be an object.');
  }
  const expected = jsonMcpServer(client, tokenEnv);
  if (owned) {
    const legacy = client === 'claude-code' && installedTokenEnv !== PROJECT_TOKEN_ENV;
    if (!isDeepStrictEqual(document.mcpServers.acceptora, jsonMcpServer(client, installedTokenEnv, legacy))) {
      throw new CliError('Project MCP config installer-owned Acceptora server has drifted.');
    }
    document.mcpServers.acceptora = expected;
  } else if (document.mcpServers.acceptora !== undefined) {
    throw new CliError('Project MCP config already defines an unmanaged Acceptora server.');
  } else {
    document.mcpServers.acceptora = expected;
  }

  if (client === 'gemini-cli') {
    document.security ??= {};
    document.security.environmentVariableRedaction ??= {};
    const redaction = document.security.environmentVariableRedaction;
    redaction.allowed ??= [];
    if (!Array.isArray(redaction.allowed) || redaction.allowed.some((name) => typeof name !== 'string')) {
      throw new CliError('Gemini environmentVariableRedaction.allowed must be an array of names.');
    }
    const occurrences = redaction.allowed.filter((name) => name === installedTokenEnv).length;
    if (owned && occurrences !== 1) {
      throw new CliError('Gemini MCP config installer-owned environment allowlist has drifted.');
    }
    if (!owned && occurrences > 0) {
      throw new CliError('Gemini MCP config already allowlists the Acceptora project variable.');
    }
    if (owned && installedTokenEnv !== tokenEnv) {
      redaction.allowed[redaction.allowed.indexOf(installedTokenEnv)] = tokenEnv;
    } else if (!owned) {
      redaction.allowed.push(tokenEnv);
    }
  }
  return { path, content: `${JSON.stringify(document, null, 2)}\n` };
}

function removeMcp(root, client, tokenEnv) {
  const profile = CLIENTS[client];
  const path = safeProjectPath(root, profile.mcpFile);
  const current = readText(path, 'Project MCP config');
  if (client === 'codex') {
    const expected = tokenEnv === PROJECT_TOKEN_ENV ? codexMcpBlock() : legacyCodexMcpBlock(tokenEnv);
    const bounds = managedBounds(current, MCP_START, MCP_END, 'Codex MCP config');
    if (bounds === null || current.slice(bounds[0], bounds[1]) !== expected) {
      throw new CliError('Codex MCP config installer-owned Acceptora block has drifted.');
    }
    const content = removeManagedBlock(current, MCP_START, MCP_END, 'Codex MCP config');
    return { path, content, empty: content.trim() === '' };
  }

  const document = parseJsonDocument(current, 'Project MCP config');
  const expected = jsonMcpServer(client, tokenEnv);
  if (!isDeepStrictEqual(document.mcpServers?.acceptora, expected)) {
    throw new CliError('Project MCP config installer-owned Acceptora server has drifted.');
  }
  delete document.mcpServers.acceptora;
  if (Object.keys(document.mcpServers).length === 0) {
    delete document.mcpServers;
  }
  if (client === 'gemini-cli') {
    const allowed = document.security?.environmentVariableRedaction?.allowed;
    if (!Array.isArray(allowed) || allowed.filter((name) => name === tokenEnv).length !== 1) {
      throw new CliError('Gemini MCP config installer-owned environment allowlist has drifted.');
    }
    allowed.splice(allowed.indexOf(tokenEnv), 1);
    if (allowed.length === 0) {
      delete document.security.environmentVariableRedaction.allowed;
    }
    if (Object.keys(document.security.environmentVariableRedaction).length === 0) {
      delete document.security.environmentVariableRedaction;
    }
    if (Object.keys(document.security).length === 0) {
      delete document.security;
    }
  }
  return { path, content: `${JSON.stringify(document, null, 2)}\n`, empty: Object.keys(document).length === 0 };
}

function configDocument(projectId) {
  return {
    project_id: projectId,
    token_env: PROJECT_TOKEN_ENV,
    origin: ACCEPTORA_ORIGIN,
    installed_version: PACKAGE_DOCUMENT.version,
  };
}

function loadConfig(root) {
  const path = safeProjectPath(root, CONFIG_PATH);
  const document = parseJsonDocument(readText(path, 'Acceptora project config'), 'Acceptora project config');
  const keys = Object.keys(document).sort();
  const legacyTokenEnv = typeof document.project_id === 'string'
    ? `ACCEPTORA_AGENT_TOKEN_${document.project_id.toUpperCase()}`
    : null;
  if (!isDeepStrictEqual(keys, ['installed_version', 'origin', 'project_id', 'token_env'])
    || !PROJECT_ID_PATTERN.test(document.project_id)
    || (document.token_env !== PROJECT_TOKEN_ENV && document.token_env !== legacyTokenEnv)
    || document.origin !== ACCEPTORA_ORIGIN
    || !VERSION_PATTERN.test(document.installed_version)) {
    throw new CliError('Acceptora project config is invalid.');
  }
  return document;
}

function resolveClient(root, explicitClient, requireInstalled) {
  if (explicitClient !== null) {
    return explicitClient;
  }
  const candidates = Object.entries(CLIENTS)
    .filter(([, profile]) => existsSync(safeProjectPath(root, profile.skillDirectory)))
    .map(([client]) => client);
  if (candidates.length === 1) {
    return candidates[0];
  }
  if (requireInstalled) {
    throw new CliError('Select the installed client with --client.');
  }
  throw new CliError('Select the coding client with --client.');
}

function installedClient(installManifest, explicitClient) {
  if (explicitClient !== null && explicitClient !== installManifest.client) {
    throw new CliError(`This project is installed for ${installManifest.client}, not ${explicitClient}.`);
  }
  return installManifest.client;
}

async function installCommand(runtime, options, root) {
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  if (existsSync(configPath) || existsSync(installManifestPath)) {
    throw new CliError('Acceptora is already installed. Use update or doctor.');
  }
  const client = resolveClient(root, options.client, false);
  const selected = await installCredential(runtime, root);
  const identity = await validateProjectKey(runtime, selected.token);

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const mcpPath = safeProjectPath(root, profile.mcpFile);
  const mcpFileExisted = existsSync(mcpPath);
  const mcp = prepareMcp(root, client, identity.tokenEnv, false);
  const snapshots = [selected.file.path, mcp.path, configPath, installManifestPath].map(snapshotFile);

  const skillMutation = replaceSkill(root, profile.skillDirectory, payload, false);
  try {
    writeProjectFile(runtime, selected.file.path, selected.content);
    writeProjectFile(runtime, mcp.path, mcp.content);
    writeProjectFile(runtime, configPath, `${JSON.stringify(configDocument(identity.projectId), null, 2)}\n`);
    writeProjectFile(runtime, installManifestPath, `${JSON.stringify(installManifestDocument(client, payload, {
      mcp: !mcpFileExisted,
    }), null, 2)}\n`);
    skillMutation.commit();
  } catch (error) {
    rollbackProjectMutation(root, skillMutation, snapshots, safeProjectPath(root, profile.skillDirectory));
    throw error;
  }

  await confirmConnection(runtime, selected.token);
  writeLine(runtime.stdout, `Acceptora installed for ${profile.label}.`);
  writeLine(runtime.stdout, `Project: ${identity.projectId}`);
  writeLine(runtime.stdout, `Project key stored in ${PROJECT_ENV_PATH}.`);
  writeLine(runtime.stdout, `Add /${PROJECT_ENV_PATH} to .gitignore before committing.`);
  writeLine(runtime.stdout, `Restart ${profile.label} to load the installed skill. Do not run install again.`);
}

async function updateCommand(runtime, options, root) {
  const config = loadConfig(root);
  const installManifest = loadInstallManifest(root);
  const client = installedClient(installManifest, options.client);
  if (config.installed_version !== installManifest.package_version) {
    throw new CliError('Acceptora config and install manifest do not match.');
  }
  const selected = await updateCredential(runtime, root, config);
  const identity = await validateProjectKey(runtime, selected.token);
  if (identity.projectId !== config.project_id) {
    throw new CliError('The project key does not match this project installation.');
  }

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const skillRoot = safeProjectPath(root, profile.skillDirectory);
  requireOwnedSkill(skillRoot, installManifest);
  const legacyInstruction = prepareLegacyInstructionRemoval(root, profile, installManifest);
  const mcp = prepareMcp(root, client, PROJECT_TOKEN_ENV, true, config.token_env);
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  const snapshots = [
    selected.file.path,
    ...(legacyInstruction === null ? [] : [legacyInstruction.path]),
    mcp.path,
    configPath,
    installManifestPath,
  ].map(snapshotFile);

  const skillMutation = replaceSkill(root, profile.skillDirectory, payload, true);
  try {
    writeProjectFile(runtime, selected.file.path, selected.content);
    applyLegacyInstructionRemoval(runtime, legacyInstruction);
    writeProjectFile(runtime, mcp.path, mcp.content);
    writeProjectFile(runtime, configPath, `${JSON.stringify(configDocument(config.project_id), null, 2)}\n`);
    writeProjectFile(runtime, installManifestPath, `${JSON.stringify(installManifestDocument(client, payload, installManifest.created_files), null, 2)}\n`);
    skillMutation.commit();
  } catch (error) {
    rollbackProjectMutation(root, skillMutation, snapshots, skillRoot);
    throw error;
  }
  await confirmConnection(runtime, selected.token);
  writeLine(runtime.stdout, `Acceptora updated for ${profile.label}. Add /${PROJECT_ENV_PATH} to .gitignore before committing.`);
}

async function doctorCommand(runtime, options, root) {
  const config = loadConfig(root);
  const installManifest = loadInstallManifest(root);
  const client = installedClient(installManifest, options.client);
  if (config.installed_version !== installManifest.package_version) {
    throw new CliError('Acceptora config and install manifest do not match.');
  }
  const token = doctorCredential(runtime, root, config);
  const identity = await validateProjectKey(runtime, token);
  if (identity.projectId !== config.project_id) {
    throw new CliError('The project key does not match this project installation.');
  }

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const skillRoot = safeProjectPath(root, profile.skillDirectory);
  requireOwnedSkill(skillRoot, installManifest);
  prepareMcp(root, client, config.token_env, true);
  const current = installManifest.package_version === PACKAGE_DOCUMENT.version
    && payloadMatches(skillRoot, payload)
    && installManifest.instruction === '';
  writeLine(runtime.stdout, current ? 'Acceptora installation is ready.' : 'Acceptora update is available.');
  writeLine(runtime.stdout, `Project: ${config.project_id}`);
  writeLine(runtime.stdout, `Client: ${profile.label}`);
  writeLine(runtime.stdout, `Package version: ${PACKAGE_DOCUMENT.version}`);
}

function removeEmptyParents(path, root) {
  let cursor = path;
  while (cursor !== root && cursor.startsWith(`${root}${sep}`) && existsSync(cursor)) {
    if (readdirSync(cursor).length > 0) {
      return;
    }
    rmdirSync(cursor);
    cursor = dirname(cursor);
  }
}

function uninstallCommand(runtime, options, root) {
  const config = loadConfig(root);
  const installManifest = loadInstallManifest(root);
  const client = installedClient(installManifest, options.client);
  if (config.installed_version !== installManifest.package_version) {
    throw new CliError('Acceptora config and install manifest do not match.');
  }
  const profile = CLIENTS[client];
  const skillRoot = safeProjectPath(root, profile.skillDirectory);
  requireOwnedSkill(skillRoot, installManifest);

  const legacyInstruction = prepareLegacyInstructionRemoval(root, profile, installManifest);
  const mcp = removeMcp(root, client, config.token_env);

  const resolvedSkillRoot = resolve(skillRoot);
  if (!resolvedSkillRoot.startsWith(`${root}${sep}`)) {
    throw new CliError('Refusing to remove a skill outside the project.');
  }
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  const snapshots = [
    ...(legacyInstruction === null ? [] : [legacyInstruction.path]),
    mcp.path,
    configPath,
    installManifestPath,
  ].map(snapshotFile);
  const skillMutation = detachSkill(root, profile.skillDirectory);
  try {
    applyLegacyInstructionRemoval(runtime, legacyInstruction);
    if (installManifest.created_files.mcp && mcp.empty) {
      unlinkSync(mcp.path);
    } else {
      writeProjectFile(runtime, mcp.path, mcp.content);
    }
    unlinkSync(configPath);
    unlinkSync(installManifestPath);
    skillMutation.commit();
  } catch (error) {
    rollbackProjectMutation(root, skillMutation, snapshots, skillRoot);
    throw error;
  }
  removeEmptyParents(dirname(configPath), root);
  removeEmptyParents(dirname(skillRoot), root);
  removeEmptyParents(dirname(mcp.path), root);
  writeLine(runtime.stdout, `Acceptora uninstalled for ${profile.label}. The project credential was not removed.`);
}

export async function main(argv = process.argv.slice(2), overrides = {}) {
  const runtime = runtimeWith(overrides);
  try {
    const options = parseArguments(argv);
    if (options.command === 'help') {
      printHelp(runtime);
      return 0;
    }
    const root = resolveProjectRoot(runtime, options.projectRoot);
    if (options.command === 'install') {
      await installCommand(runtime, options, root);
    } else if (options.command === 'update') {
      await updateCommand(runtime, options, root);
    } else if (options.command === 'doctor') {
      await doctorCommand(runtime, options, root);
    } else {
      uninstallCommand(runtime, options, root);
    }
    return 0;
  } catch (error) {
    if (error instanceof CancelledError) {
      writeLine(runtime.stderr, 'Acceptora installation cancelled.');
      return 130;
    }
    const message = error instanceof CliError ? error.message : 'Acceptora installer failed safely.';
    writeLine(runtime.stderr, `Acceptora installer failed: ${message}`);
    return error instanceof CliError ? 2 : 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
