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
import { createInterface } from 'node:readline/promises';
import { isDeepStrictEqual } from 'node:util';
import { createHash, randomUUID } from 'node:crypto';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_DOCUMENT = JSON.parse(readFileSync(join(PACKAGE_ROOT, 'package.json'), 'utf8'));
const ACCEPTORA_ORIGIN = 'https://www.acceptora.com';
const PROJECT_URL = `${ACCEPTORA_ORIGIN}/api/v1/integrations/project`;
const CONFIG_PATH = '.acceptora/config.json';
const INSTALL_MANIFEST_PATH = '.acceptora/install-manifest.json';
const TOKEN_PATTERN = /^avt_(?<ulid>[0-9A-HJKMNP-TV-Z]{26})_[A-Za-z0-9]{48}$/;
const TOKEN_ENV_PATTERN = /^ACCEPTORA_AGENT_TOKEN_PROJ_[0-9A-HJKMNP-TV-Z]{26}$/;
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
    instructionFile: 'AGENTS.md',
    mcpFile: '.codex/config.toml',
  },
  'claude-code': {
    label: 'Claude Code',
    skillDirectory: '.claude/skills/acceptora',
    instructionFile: 'CLAUDE.md',
    mcpFile: '.mcp.json',
  },
  'gemini-cli': {
    label: 'Gemini CLI',
    skillDirectory: '.gemini/skills/acceptora',
    instructionFile: 'GEMINI.md',
    mcpFile: '.gemini/settings.json',
  },
};
const SKILL_PAYLOAD = [
  'SKILL.md',
  'agents/openai.yaml',
  'references/api-mcp.md',
  'scripts/project_context.py',
];
const INSTRUCTION_START = '<!-- acceptora:start -->';
const INSTRUCTION_END = '<!-- acceptora:end -->';
const PROJECT_INSTRUCTION = `${INSTRUCTION_START} Before implementation work or manual-verification changes, use the project-local Acceptora skill. It fetches fresh project instructions and synchronizes verification after eligible changes. ${INSTRUCTION_END}`;
const MCP_START = '# acceptora-mcp:start';
const MCP_END = '# acceptora-mcp:end';
const ENV_TEMPLATE_PARTS = new Set(['default', 'defaults', 'dist', 'example', 'sample', 'template']);
const INSTRUCTION_FIELDS = ['analysis_guidance', 'manual_verification_guidance', 'test_data_guidance'];
const INSTRUCTION_SOURCES = new Set(['default', 'account', 'project']);
const MAX_RESPONSE_BYTES = 1_048_576;
const MAX_INSTRUCTION_CHARACTERS = 12_000;

export class CliError extends Error {}

class CancelledError extends Error {}

class SetupIncompleteError extends Error {}

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
    askLine: null,
    storeWindows: null,
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

  const options = { command, client: null, projectRoot: null, tokenEnv: null };
  while (values.length > 0) {
    const flag = values.shift();
    if (!['--client', '--project-root', '--token-env'].includes(flag)) {
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
    } else {
      options.tokenEnv = value;
    }
  }
  if (options.client !== null && CLIENTS[options.client] === undefined) {
    throw new CliError('Unsupported coding client.');
  }
  if (options.tokenEnv !== null && !TOKEN_ENV_PATTERN.test(options.tokenEnv)) {
    throw new CliError('--token-env must name one project-scoped Acceptora variable.');
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
    return normalized !== 'ACCEPTORA_AGENT_TOKEN' && !normalized.startsWith('ACCEPTORA_AGENT_TOKEN_');
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

function credentialIdentity(token) {
  const match = TOKEN_PATTERN.exec(token);
  if (match === null) {
    throw new CliError('The supplied value is not a valid Acceptora project key.');
  }
  const ulid = match.groups.ulid;
  return {
    projectId: `proj_${ulid}`,
    tokenEnv: `ACCEPTORA_AGENT_TOKEN_PROJ_${ulid}`,
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

function validateVerificationInstructions(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value) || value.schema_version !== '1.0') {
    throw new CliError('Acceptora returned invalid verification instructions.');
  }
  for (const revision of [value.account_revision, value.project_revision]) {
    if (!Number.isSafeInteger(revision) || revision < 0) {
      throw new CliError('Acceptora returned invalid verification instructions.');
    }
  }
  if (typeof value.configured !== 'boolean'
    || value.instructions === null
    || typeof value.instructions !== 'object'
    || Array.isArray(value.instructions)
    || value.sources === null
    || typeof value.sources !== 'object'
    || Array.isArray(value.sources)
    || typeof value.effective_digest !== 'string'
    || !DIGEST_PATTERN.test(value.effective_digest)) {
    throw new CliError('Acceptora returned invalid verification instructions.');
  }
  let configured = false;
  for (const field of INSTRUCTION_FIELDS) {
    const instruction = value.instructions[field];
    const source = value.sources[field];
    if (instruction !== null && (typeof instruction !== 'string'
      || instruction.length > MAX_INSTRUCTION_CHARACTERS
      || instruction.replaceAll('\r\n', '\n').replaceAll('\r', '\n').replace(/^[ \t\n\r\0\v]+|[ \t\n\r\0\v]+$/g, '') !== instruction)) {
      throw new CliError('Acceptora returned invalid verification instructions.');
    }
    if (typeof source !== 'string' || !INSTRUCTION_SOURCES.has(source) || ((instruction === null) !== (source === 'default'))) {
      throw new CliError('Acceptora returned invalid verification instructions.');
    }
    configured ||= instruction !== null;
  }
  if (configured !== value.configured) {
    throw new CliError('Acceptora returned invalid verification instructions.');
  }
}

async function validateProjectKey(runtime, token) {
  const identity = credentialIdentity(token);
  const payload = await fetchJson(runtime, PROJECT_URL, {
    method: 'GET',
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
      'User-Agent': `Acceptora-Agent-Skill/${PACKAGE_DOCUMENT.version}`,
    },
  }, 'Acceptora project verification failed.');
  if (payload === null || typeof payload !== 'object' || payload.project_id !== identity.projectId) {
    throw new CliError('The project key does not match the authenticated Acceptora project.');
  }
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
  validateVerificationInstructions(payload.verification_instructions);
  return identity;
}

async function hiddenPrompt(runtime) {
  if (runtime.readSecret !== null) {
    return runtime.readSecret();
  }
  const { stdin, stdout } = runtime;
  if (!stdin.isTTY || !stdout.isTTY || typeof stdin.setRawMode !== 'function') {
    throw new CliError('Run the installer in an interactive terminal so the project key can be entered securely.');
  }
  stdout.write('Acceptora project key: ');
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
          value = value.slice(0, -1);
        } else if (value.length < 255) {
          value += character;
        }
      }
    };
    stdin.setEncoding('utf8');
    stdin.setRawMode(true);
    stdin.resume();
    stdin.on('data', onData);
  });
}

async function linePrompt(runtime, question) {
  if (runtime.askLine !== null) {
    return runtime.askLine(question);
  }
  if (!runtime.stdin.isTTY || !runtime.stdout.isTTY) {
    throw new CliError('This choice requires an interactive terminal.');
  }
  const interface_ = createInterface({ input: runtime.stdin, output: runtime.stdout });
  try {
    return await interface_.question(question);
  } finally {
    interface_.close();
  }
}

async function selectNamedValue(runtime, names, question) {
  names.forEach((name, index) => writeLine(runtime.stdout, `  ${index + 1}. ${name}`));
  const answer = (await linePrompt(runtime, question)).trim();
  const selected = Number.parseInt(answer, 10);
  if (!Number.isInteger(selected) || selected < 1 || selected > names.length) {
    throw new CliError('No valid selection was made.');
  }
  return names[selected - 1];
}

async function selectCredential(runtime, explicitTokenEnv = null) {
  if (explicitTokenEnv !== null && runtime.env[explicitTokenEnv]) {
    return { token: runtime.env[explicitTokenEnv], tokenEnv: explicitTokenEnv, fromEnvironment: true };
  }
  if (explicitTokenEnv !== null) {
    const token = await hiddenPrompt(runtime);
    const identity = credentialIdentity(token);
    if (identity.tokenEnv !== explicitTokenEnv) {
      throw new CliError('The supplied project key does not match --token-env.');
    }
    return { token, tokenEnv: explicitTokenEnv, fromEnvironment: false };
  }

  const candidates = Object.keys(runtime.env).filter((name) => TOKEN_ENV_PATTERN.test(name) && runtime.env[name]);
  candidates.sort();
  if (candidates.length === 1) {
    return { token: runtime.env[candidates[0]], tokenEnv: candidates[0], fromEnvironment: true };
  }
  if (candidates.length > 1) {
    const selected = await selectNamedValue(runtime, candidates, 'Select the project variable to use: ');
    return { token: runtime.env[selected], tokenEnv: selected, fromEnvironment: true };
  }
  const token = await hiddenPrompt(runtime);
  return { token, tokenEnv: credentialIdentity(token).tokenEnv, fromEnvironment: false };
}

function isEnvironmentFilename(name) {
  const normalized = name.toLowerCase();
  const parts = normalized.split(/[._-]+/).filter(Boolean);
  if (parts.some((part) => ENV_TEMPLATE_PARTS.has(part))) {
    return false;
  }
  return normalized === '.env'
    || normalized === '.envrc'
    || normalized === '.dev.vars'
    || normalized.startsWith('.env.')
    || normalized.startsWith('.dev.vars.')
    || normalized.endsWith('.env');
}

function inspectEnvironmentStores(runtime, root) {
  const candidates = readdirSync(root).filter(isEnvironmentFilename).sort();
  const safe = [];
  for (const name of candidates) {
    const path = join(root, name);
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      continue;
    }
    const ignored = runGit(runtime, root, ['check-ignore', '--quiet', '--', name], [0, 1]).status === 0;
    const tracked = runGit(runtime, root, ['ls-files', '--error-unmatch', '--', name], [0, 1]).status === 0;
    if (ignored && !tracked) {
      safe.push(name);
    }
  }
  return safe;
}

function storeWindowsCredential(runtime, name, token) {
  if (runtime.storeWindows !== null) {
    runtime.storeWindows(name, token);
    return;
  }
  const script = [
    '$name=[Console]::In.ReadLine()',
    '$value=[Console]::In.ReadToEnd()',
    '$signature=\'[DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd,uint Msg,UIntPtr wParam,string lParam,uint flags,uint timeout,out UIntPtr result);\'',
    'try{Add-Type -Namespace Acceptora -Name NativeMethods -MemberDefinition $signature}catch{exit 3}',
    '$key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Environment")',
    '$previous=$key.GetValue($name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)',
    '$hadPrevious=$null -ne $previous',
    '$previousKind=if($hadPrevious){$key.GetValueKind($name)}else{$null}',
    'try{$key.SetValue($name,$value,[Microsoft.Win32.RegistryValueKind]::String);if($key.GetValue($name)-ne $value){throw "write"};$broadcast=[UIntPtr]::Zero;$sent=[Acceptora.NativeMethods]::SendMessageTimeout([IntPtr]0xffff,0x001A,[UIntPtr]::Zero,"Environment",0x0002,5000,[ref]$broadcast);if($sent -eq [IntPtr]::Zero){throw "broadcast"}}catch{if($hadPrevious){$key.SetValue($name,$previous,$previousKind)}else{$key.DeleteValue($name,$false)};exit 2}finally{$key.Dispose()}',
  ].join(';');
  const result = runtime.spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8',
    env: secretFreeEnvironment(runtime),
    input: `${name}\n${token}`,
    timeout: 20_000,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw new CliError('Windows did not confirm the current-user environment update.');
  }
}

async function persistPromptedCredential(runtime, root, identity, token) {
  const stores = inspectEnvironmentStores(runtime, root);
  if (stores.length > 0) {
    const store = stores.length === 1
      ? stores[0]
      : await selectNamedValue(runtime, stores, 'Select the environment file this project loads: ');
    throw new SetupIncompleteError(
      `Project key validated for ${identity.projectId}. Add ${identity.tokenEnv} to ${store}, restart the coding client through the project's existing environment loader, then run this install command again. The installer did not read or modify the environment file.`,
    );
  }

  if (runtime.platform === 'win32') {
    writeLine(runtime.stdout, 'No ignored project environment file was found. A Windows current-user environment variable is readable by other processes running as this OS user.');
    const answer = (await linePrompt(runtime, `Store ${identity.tokenEnv} in the Windows current-user environment? [y/N] `)).trim().toLowerCase();
    if (answer === 'y' || answer === 'yes') {
      storeWindowsCredential(runtime, identity.tokenEnv, token);
      throw new SetupIncompleteError(`Project key validated and stored as ${identity.tokenEnv}. Restart the coding client, then run this install command again.`);
    }
  }

  throw new SetupIncompleteError(`Project key validated for ${identity.projectId}. Configure ${identity.tokenEnv} through this project's secret loader, restart the coding client, then run this install command again.`);
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
    instruction: PROJECT_INSTRUCTION,
    created_files: createdFiles,
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
    || !document.instruction.startsWith(INSTRUCTION_START)
    || !document.instruction.endsWith(INSTRUCTION_END)
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

function codexMcpBlock(tokenEnv) {
  return `${MCP_START}\n[mcp_servers.acceptora]\nurl = "${ACCEPTORA_ORIGIN}/mcp"\nbearer_token_env_var = "${tokenEnv}"\n${MCP_END}`;
}

function jsonMcpServer(client, tokenEnv) {
  const authorization = { Authorization: `Bearer \${${tokenEnv}}` };
  if (client === 'claude-code') {
    return { type: 'http', url: `${ACCEPTORA_ORIGIN}/mcp`, headers: authorization };
  }
  return { type: 'http', url: `${ACCEPTORA_ORIGIN}/mcp`, headers: authorization };
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

function prepareMcp(root, client, tokenEnv, owned) {
  const profile = CLIENTS[client];
  const path = safeProjectPath(root, profile.mcpFile);
  const current = readText(path, 'Project MCP config');
  if (client === 'codex') {
    const expected = codexMcpBlock(tokenEnv);
    const bounds = managedBounds(current, MCP_START, MCP_END, 'Codex MCP config');
    if (owned) {
      if (bounds === null || current.slice(bounds[0], bounds[1]) !== expected) {
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
    if (!isDeepStrictEqual(document.mcpServers.acceptora, expected)) {
      throw new CliError('Project MCP config installer-owned Acceptora server has drifted.');
    }
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
    const occurrences = redaction.allowed.filter((name) => name === tokenEnv).length;
    if (owned && occurrences !== 1) {
      throw new CliError('Gemini MCP config installer-owned environment allowlist has drifted.');
    }
    if (!owned && occurrences > 0) {
      throw new CliError('Gemini MCP config already allowlists the Acceptora project variable.');
    }
    if (!owned) {
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
    const expected = codexMcpBlock(tokenEnv);
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

function configDocument(projectId, tokenEnv) {
  return {
    project_id: projectId,
    token_env: tokenEnv,
    origin: ACCEPTORA_ORIGIN,
    installed_version: PACKAGE_DOCUMENT.version,
  };
}

function loadConfig(root) {
  const path = safeProjectPath(root, CONFIG_PATH);
  const document = parseJsonDocument(readText(path, 'Acceptora project config'), 'Acceptora project config');
  const keys = Object.keys(document).sort();
  if (!isDeepStrictEqual(keys, ['installed_version', 'origin', 'project_id', 'token_env'])
    || !PROJECT_ID_PATTERN.test(document.project_id)
    || document.token_env !== `ACCEPTORA_AGENT_TOKEN_${document.project_id.toUpperCase()}`
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

async function installedCredential(runtime, config, explicitTokenEnv) {
  if (explicitTokenEnv !== null && explicitTokenEnv !== config.token_env) {
    throw new CliError('--token-env does not match this project installation.');
  }
  const visible = runtime.env[config.token_env];
  if (visible) {
    return { token: visible, fromEnvironment: true };
  }
  const token = await hiddenPrompt(runtime);
  const identity = credentialIdentity(token);
  if (identity.projectId !== config.project_id || identity.tokenEnv !== config.token_env) {
    throw new CliError('The supplied project key does not match this project installation.');
  }
  return { token, fromEnvironment: false };
}

function visibleInstalledCredential(runtime, config, explicitTokenEnv) {
  if (explicitTokenEnv !== null && explicitTokenEnv !== config.token_env) {
    throw new CliError('--token-env does not match this project installation.');
  }
  const token = runtime.env[config.token_env];
  if (!token) {
    throw new CliError(`${config.token_env} is not available in this process. Restart the coding client after configuring it.`);
  }
  return token;
}

async function installCommand(runtime, options, root) {
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  if (existsSync(configPath) || existsSync(installManifestPath)) {
    throw new CliError('Acceptora is already installed. Use update or doctor.');
  }
  const client = resolveClient(root, options.client, false);
  const selected = await selectCredential(runtime, options.tokenEnv);
  const identity = await validateProjectKey(runtime, selected.token);
  if (selected.tokenEnv !== identity.tokenEnv) {
    throw new CliError('The selected environment variable does not match the project key.');
  }
  if (!selected.fromEnvironment) {
    await persistPromptedCredential(runtime, root, identity, selected.token);
  }

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const instructionPath = safeProjectPath(root, profile.instructionFile);
  const instructionFileExisted = existsSync(instructionPath);
  const instruction = upsertManagedBlock(
    readText(instructionPath, 'Project instruction file'),
    PROJECT_INSTRUCTION,
    INSTRUCTION_START,
    INSTRUCTION_END,
    'Project instruction file',
  );
  const mcpPath = safeProjectPath(root, profile.mcpFile);
  const mcpFileExisted = existsSync(mcpPath);
  const mcp = prepareMcp(root, client, identity.tokenEnv, false);
  const snapshots = [instructionPath, mcp.path, configPath, installManifestPath].map(snapshotFile);

  const skillMutation = replaceSkill(root, profile.skillDirectory, payload, false);
  try {
    writeProjectFile(runtime, instructionPath, instruction);
    writeProjectFile(runtime, mcp.path, mcp.content);
    writeProjectFile(runtime, configPath, `${JSON.stringify(configDocument(identity.projectId, identity.tokenEnv), null, 2)}\n`);
    writeProjectFile(runtime, installManifestPath, `${JSON.stringify(installManifestDocument(client, payload, {
      instruction: !instructionFileExisted,
      mcp: !mcpFileExisted,
    }), null, 2)}\n`);
    skillMutation.commit();
  } catch (error) {
    rollbackProjectMutation(root, skillMutation, snapshots, safeProjectPath(root, profile.skillDirectory));
    throw error;
  }

  writeLine(runtime.stdout, `Acceptora installed for ${profile.label}.`);
  writeLine(runtime.stdout, `Project: ${identity.projectId}`);
  writeLine(runtime.stdout, `Credential variable: ${identity.tokenEnv}`);
  writeLine(runtime.stdout, `Restart ${profile.label} before using the Acceptora skill.`);
}

async function updateCommand(runtime, options, root) {
  const config = loadConfig(root);
  const installManifest = loadInstallManifest(root);
  const client = installedClient(installManifest, options.client);
  if (config.installed_version !== installManifest.package_version) {
    throw new CliError('Acceptora config and install manifest do not match.');
  }
  const selected = await installedCredential(runtime, config, options.tokenEnv);
  const identity = await validateProjectKey(runtime, selected.token);
  if (identity.projectId !== config.project_id || identity.tokenEnv !== config.token_env) {
    throw new CliError('The project key does not match this project installation.');
  }
  if (!selected.fromEnvironment) {
    await persistPromptedCredential(runtime, root, identity, selected.token);
  }

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const skillRoot = safeProjectPath(root, profile.skillDirectory);
  requireOwnedSkill(skillRoot, installManifest);
  const instructionPath = safeProjectPath(root, profile.instructionFile);
  const currentInstruction = readText(instructionPath, 'Project instruction file');
  const instructionBounds = managedBounds(currentInstruction, INSTRUCTION_START, INSTRUCTION_END, 'Project instruction file');
  if (instructionBounds === null || currentInstruction.slice(instructionBounds[0], instructionBounds[1]) !== installManifest.instruction) {
    throw new CliError('Project instruction file installer-owned Acceptora block has drifted.');
  }
  const instruction = upsertManagedBlock(
    currentInstruction,
    PROJECT_INSTRUCTION,
    INSTRUCTION_START,
    INSTRUCTION_END,
    'Project instruction file',
  );
  const mcp = prepareMcp(root, client, config.token_env, true);
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  const snapshots = [instructionPath, mcp.path, configPath, installManifestPath].map(snapshotFile);

  const skillMutation = replaceSkill(root, profile.skillDirectory, payload, true);
  try {
    writeProjectFile(runtime, instructionPath, instruction);
    writeProjectFile(runtime, mcp.path, mcp.content);
    writeProjectFile(runtime, configPath, `${JSON.stringify(configDocument(config.project_id, config.token_env), null, 2)}\n`);
    writeProjectFile(runtime, installManifestPath, `${JSON.stringify(installManifestDocument(client, payload, installManifest.created_files), null, 2)}\n`);
    skillMutation.commit();
  } catch (error) {
    rollbackProjectMutation(root, skillMutation, snapshots, skillRoot);
    throw error;
  }
  writeLine(runtime.stdout, `Acceptora updated for ${profile.label}. Restart the client before continuing.`);
}

async function doctorCommand(runtime, options, root) {
  const config = loadConfig(root);
  const installManifest = loadInstallManifest(root);
  const client = installedClient(installManifest, options.client);
  if (config.installed_version !== installManifest.package_version) {
    throw new CliError('Acceptora config and install manifest do not match.');
  }
  const token = visibleInstalledCredential(runtime, config, options.tokenEnv);
  const identity = await validateProjectKey(runtime, token);
  if (identity.projectId !== config.project_id || identity.tokenEnv !== config.token_env) {
    throw new CliError('The project key does not match this project installation.');
  }

  const profile = CLIENTS[client];
  const payload = expectedPayload();
  const skillRoot = safeProjectPath(root, profile.skillDirectory);
  const instruction = readText(safeProjectPath(root, profile.instructionFile), 'Project instruction file');
  requireOwnedSkill(skillRoot, installManifest);
  prepareMcp(root, client, config.token_env, true);
  const instructionBounds = managedBounds(instruction, INSTRUCTION_START, INSTRUCTION_END, 'Project instruction file');
  if (instructionBounds === null || instruction.slice(instructionBounds[0], instructionBounds[1]) !== installManifest.instruction) {
    throw new CliError('Project instruction file is missing its installer-owned Acceptora block.');
  }
  const current = installManifest.package_version === PACKAGE_DOCUMENT.version
    && payloadMatches(skillRoot, payload)
    && installManifest.instruction === PROJECT_INSTRUCTION;
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

  const instructionPath = safeProjectPath(root, profile.instructionFile);
  const currentInstruction = readText(instructionPath, 'Project instruction file');
  const instructionBounds = managedBounds(currentInstruction, INSTRUCTION_START, INSTRUCTION_END, 'Project instruction file');
  if (instructionBounds === null || currentInstruction.slice(instructionBounds[0], instructionBounds[1]) !== installManifest.instruction) {
    throw new CliError('Project instruction file installer-owned Acceptora block has drifted.');
  }
  const instruction = removeManagedBlock(
    currentInstruction,
    INSTRUCTION_START,
    INSTRUCTION_END,
    'Project instruction file',
  );
  const mcp = removeMcp(root, client, config.token_env);

  const resolvedSkillRoot = resolve(skillRoot);
  if (!resolvedSkillRoot.startsWith(`${root}${sep}`)) {
    throw new CliError('Refusing to remove a skill outside the project.');
  }
  const configPath = safeProjectPath(root, CONFIG_PATH);
  const installManifestPath = safeProjectPath(root, INSTALL_MANIFEST_PATH);
  const snapshots = [instructionPath, mcp.path, configPath, installManifestPath].map(snapshotFile);
  const skillMutation = detachSkill(root, profile.skillDirectory);
  try {
    if (installManifest.created_files.instruction && instruction.trim() === '') {
      unlinkSync(instructionPath);
    } else {
      writeProjectFile(runtime, instructionPath, instruction);
    }
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
  removeEmptyParents(dirname(instructionPath), root);
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
    if (error instanceof SetupIncompleteError) {
      writeLine(runtime.stdout, error.message);
      return 2;
    }
    const message = error instanceof CliError ? error.message : 'Acceptora installer failed safely.';
    writeLine(runtime.stderr, `Acceptora installer failed: ${message}`);
    return error instanceof CliError ? 2 : 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
