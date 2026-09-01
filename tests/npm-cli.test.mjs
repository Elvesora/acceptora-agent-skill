import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import {
  existsSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative } from 'node:path';
import { EventEmitter } from 'node:events';
import test from 'node:test';

import { main } from '../cli/acceptora-agent-skill.mjs';

const PROJECT_ULID = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
const CREDENTIAL_ULID = '01ARZ3NDEKTSV4RRFFQ69G5FAA';
const PROJECT_ID = `proj_${PROJECT_ULID}`;
const TOKEN_ENV = `ACCEPTORA_AGENT_TOKEN_PROJ_${PROJECT_ULID}`;
const TOKEN = `avt_${CREDENTIAL_ULID}_${'A'.repeat(48)}`;
const SCOPES = [
  'projects:read',
  'features:resolve',
  'features:read',
  'checklists:write',
  'feedback:read',
  'feedback:address',
  'gates:read',
];

class MemoryStream {
  constructor() {
    this.value = '';
    this.isTTY = false;
  }

  write(chunk) {
    this.value += String(chunk);
    return true;
  }
}

class InteractiveInput extends EventEmitter {
  constructor() {
    super();
    this.isTTY = true;
    this.isRaw = false;
  }

  setEncoding() {}

  setRawMode(value) {
    this.isRaw = value;
  }

  resume() {}

  pause() {}
}

function createProject(files = {}) {
  const root = mkdtempSync(join(tmpdir(), 'acceptora-npm-cli-'));
  const initialized = spawnSync('git', ['init', '--quiet', root], { encoding: 'utf8' });
  assert.equal(initialized.status, 0, initialized.stderr);
  for (const [relativePath, body] of Object.entries(files)) {
    const path = join(root, relativePath);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, body, 'utf8');
  }
  return root;
}

function projectResponse(projectId = PROJECT_ID) {
  return new Response(JSON.stringify({
    project_id: projectId,
    granted_scopes: SCOPES,
    verification_instructions: {
      schema_version: '1.0',
      account_revision: 0,
      project_revision: 0,
      effective_digest: `sha256:${'0'.repeat(64)}`,
      configured: false,
      instructions: {
        analysis_guidance: null,
        manual_verification_guidance: null,
        test_data_guidance: null,
      },
      sources: {
        analysis_guidance: 'default',
        manual_verification_guidance: 'default',
        test_data_guidance: 'default',
      },
    },
  }), { status: 200 });
}

function fakeFetch(url) {
  if (String(url).endsWith('/api/v1/integrations/project')) {
    return Promise.resolve(projectResponse());
  }
  throw new Error(`Unexpected URL: ${url}`);
}

function testRuntime(root, overrides = {}) {
  const stdout = new MemoryStream();
  const stderr = new MemoryStream();
  return {
    runtime: {
      cwd: root,
      env: { [TOKEN_ENV]: TOKEN },
      platform: 'linux',
      stdout,
      stderr,
      fetch: fakeFetch,
      ...overrides,
    },
    stdout,
    stderr,
  };
}

function allFileText(root) {
  const values = [];
  const visit = (directory) => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      const metadata = lstatSync(path);
      if (metadata.isDirectory()) {
        if (name !== '.git') {
          visit(path);
        }
      } else if (metadata.isFile()) {
        values.push(`${relative(root, path)}\n${readFileSync(path, 'utf8')}`);
      }
    }
  };
  visit(root);
  return values.join('\n');
}

test('install writes only the selected client surfaces and never persists the secret', async (context) => {
  const clients = {
    codex: ['.agents/skills/acceptora/SKILL.md', 'AGENTS.md', '.codex/config.toml'],
    'claude-code': ['.claude/skills/acceptora/SKILL.md', 'CLAUDE.md', '.mcp.json'],
    'gemini-cli': ['.gemini/skills/acceptora/SKILL.md', 'GEMINI.md', '.gemini/settings.json'],
  };

  for (const [client, paths] of Object.entries(clients)) {
    await context.test(client, async () => {
      const root = createProject();
      try {
        const { runtime, stdout, stderr } = testRuntime(root);
        const status = await main(['install', '--client', client], runtime);

        assert.equal(status, 0, stderr.value);
        paths.forEach((path) => assert.equal(existsSync(join(root, path)), true, path));
        assert.equal(existsSync(join(root, '.acceptora/config.json')), true);
        assert.equal(existsSync(join(root, '.acceptora/install-manifest.json')), true);
        const config = JSON.parse(readFileSync(join(root, '.acceptora/config.json'), 'utf8'));
        assert.equal(config.project_id, PROJECT_ID);
        assert.equal(config.token_env, TOKEN_ENV);
        assert.equal(config.installed_version, '1.0.1');
        const manifest = JSON.parse(readFileSync(join(root, '.acceptora/install-manifest.json'), 'utf8'));
        assert.equal(manifest.client, client);
        assert.equal(manifest.package_version, '1.0.1');
        assert.equal(Object.keys(manifest.payload).length, 4);
        assert.doesNotMatch(`${stdout.value}${stderr.value}${allFileText(root)}`, new RegExp(TOKEN));

        if (client === 'claude-code' || client === 'gemini-cli') {
          const mcp = JSON.parse(readFileSync(join(root, paths[2]), 'utf8'));
          assert.equal(mcp.mcpServers.acceptora.headers.Authorization, `Bearer \${${TOKEN_ENV}}`);
        }
        if (client === 'gemini-cli') {
          const settings = JSON.parse(readFileSync(join(root, '.gemini/settings.json'), 'utf8'));
          assert.deepEqual(settings.security.environmentVariableRedaction.allowed, [TOKEN_ENV]);
        }
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test('interactive project key paste shows masking without echoing the secret', async () => {
  const root = createProject();
  try {
    const stdin = new InteractiveInput();
    const { runtime, stdout, stderr } = testRuntime(root, { env: {}, stdin });
    stdout.isTTY = true;

    const installation = main(['install', '--client', 'codex'], runtime);
    queueMicrotask(() => stdin.emit('data', `${TOKEN}\r`));
    const status = await installation;

    assert.equal(status, 2, stderr.value);
    assert.match(stdout.value, /Acceptora project key \(hidden\): /);
    assert.match(stdout.value, new RegExp(`\\*{${TOKEN.length}}`));
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(stdin.isRaw, false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('missing credential is requested before installation and a safe env file is never read or written', async () => {
  const root = createProject({ '.gitignore': '.env\n', '.env': 'EXISTING=value\n' });
  try {
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      readSecret: async () => TOKEN,
    });
    const status = await main(['install', '--client', 'codex'], runtime);

    assert.equal(status, 2, stderr.value);
    assert.match(stdout.value, new RegExp(TOKEN_ENV));
    assert.match(stdout.value, /\.env/);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(readFileSync(join(root, '.env'), 'utf8'), 'EXISTING=value\n');
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
    assert.equal(existsSync(join(root, 'AGENTS.md')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('tracked environment files are ignored as credential stores without blocking guidance', async () => {
  const root = createProject({ '.env.testing': 'TRACKED=value\n' });
  try {
    const tracked = spawnSync('git', ['-C', root, 'add', '.env.testing'], { encoding: 'utf8' });
    assert.equal(tracked.status, 0, tracked.stderr);
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      readSecret: async () => TOKEN,
    });
    const status = await main(['install', '--client', 'codex'], runtime);

    assert.equal(status, 2, stderr.value);
    assert.match(stdout.value, new RegExp(TOKEN_ENV));
    assert.match(stdout.value, /secret loader/);
    assert.equal(readFileSync(join(root, '.env.testing'), 'utf8'), 'TRACKED=value\n');
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('invalid project key fails before any project write and is not echoed', async () => {
  const root = createProject();
  try {
    const invalid = 'not-a-project-key';
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      readSecret: async () => invalid,
    });
    const status = await main(['install', '--client', 'codex'], runtime);

    assert.equal(status, 2);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(invalid));
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('doctor, update, and uninstall preserve unrelated project configuration', async () => {
  const root = createProject({
    'CLAUDE.md': 'User instruction.\n',
    '.mcp.json': `${JSON.stringify({ setting: true, mcpServers: { other: { url: 'https://example.test/mcp' } } }, null, 2)}\n`,
  });
  try {
    const { runtime, stdout, stderr } = testRuntime(root);
    assert.equal(await main(['install', '--client', 'claude-code'], runtime), 0, stderr.value);
    assert.equal(await main(['doctor'], runtime), 0, stderr.value);
    assert.match(stdout.value, /installation is ready/);
    assert.equal(await main(['update'], runtime), 0, stderr.value);
    assert.equal(await main(['uninstall'], runtime), 0, stderr.value);

    assert.equal(existsSync(join(root, '.claude/skills/acceptora')), false);
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(readFileSync(join(root, 'CLAUDE.md'), 'utf8'), 'User instruction.\n');
    const mcp = JSON.parse(readFileSync(join(root, '.mcp.json'), 'utf8'));
    assert.equal(mcp.setting, true);
    assert.deepEqual(mcp.mcpServers.other, { url: 'https://example.test/mcp' });
    assert.equal(mcp.mcpServers.acceptora, undefined);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('doctor and uninstall fail closed when an owned skill file drifts', async () => {
  const root = createProject();
  try {
    const { runtime, stderr } = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], runtime), 0, stderr.value);
    writeFileSync(join(root, '.agents/skills/acceptora/SKILL.md'), 'local drift\n', 'utf8');

    assert.equal(await main(['doctor'], runtime), 2);
    assert.match(stderr.value, /has drifted/);
    assert.equal(await main(['uninstall'], runtime), 2);
    assert.equal(existsSync(join(root, '.agents/skills/acceptora')), true);
    assert.equal(existsSync(join(root, '.acceptora/config.json')), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('doctor is read-only when the installed project variable is missing', async () => {
  const root = createProject();
  try {
    const installed = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], installed.runtime), 0, installed.stderr.value);

    let prompted = false;
    let stored = false;
    const diagnosis = testRuntime(root, {
      env: {},
      readSecret: async () => {
        prompted = true;
        return TOKEN;
      },
      storeWindows: () => {
        stored = true;
      },
    });
    assert.equal(await main(['doctor'], diagnosis.runtime), 2);
    assert.match(diagnosis.stderr.value, new RegExp(TOKEN_ENV));
    assert.equal(prompted, false);
    assert.equal(stored, false);
    assert.doesNotMatch(`${diagnosis.stdout.value}${diagnosis.stderr.value}`, new RegExp(TOKEN));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('token-shaped command arguments are never echoed', async () => {
  const root = createProject();
  try {
    const { runtime, stdout, stderr } = testRuntime(root);
    assert.equal(await main([TOKEN], runtime), 2);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('install rejects invalid verification instructions before writing', async () => {
  const root = createProject();
  try {
    const { runtime, stderr } = testRuntime(root, {
      fetch: (url, options) => String(url).endsWith('/api/v1/integrations/project')
        ? Promise.resolve(new Response(JSON.stringify({
          project_id: PROJECT_ID,
          granted_scopes: SCOPES,
          verification_instructions: { schema_version: '1.0' },
        }), { status: 200 }))
        : fakeFetch(url, options),
    });
    assert.equal(await main(['install', '--client', 'codex'], runtime), 2);
    assert.match(stderr.value, /invalid verification instructions/);
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('Git inspection receives no Acceptora credential variables', async () => {
  const root = createProject();
  try {
    const childEnvironments = [];
    const { runtime, stderr } = testRuntime(root, {
      spawnSync: (command, arguments_, options) => {
        childEnvironments.push(options.env);
        return spawnSync(command, arguments_, options);
      },
    });
    assert.equal(await main(['install', '--client', 'codex'], runtime), 0, stderr.value);
    assert.ok(childEnvironments.length > 0);
    for (const environment of childEnvironments) {
      assert.equal(environment[TOKEN_ENV], undefined);
      assert.equal(environment.ACCEPTORA_AGENT_TOKEN, undefined);
      assert.equal(environment.GIT_TERMINAL_PROMPT, '0');
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('Windows credential storage stops for restart without partial installation', async () => {
  const root = createProject();
  try {
    let powershellScript = null;
    let powershellInput = null;
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      platform: 'win32',
      readSecret: async () => TOKEN,
      askLine: async () => 'yes',
      spawnSync: (command, arguments_, options) => {
        if (command === 'powershell.exe') {
          powershellScript = arguments_.at(-1);
          powershellInput = options.input;
          return { status: 0, stdout: '', stderr: '' };
        }
        return spawnSync(command, arguments_, options);
      },
    });
    assert.equal(await main(['install', '--client', 'codex'], runtime), 2, stderr.value);
    assert.equal(powershellInput, `${TOKEN_ENV}\n${TOKEN}`);
    assert.match(powershellScript, /SendMessageTimeout/);
    assert.match(powershellScript, /previousKind/);
    assert.doesNotMatch(powershellScript, /catch\{\}/);
    if (process.platform === 'win32') {
      const parsed = spawnSync('powershell.exe', [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        '$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count -gt 0){exit 1}',
      ], { encoding: 'utf8', input: powershellScript });
      assert.equal(parsed.status, 0, parsed.stderr);
    }
    assert.match(stdout.value, /Restart the coding client/);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);

    const failed = testRuntime(root, {
      env: {},
      platform: 'win32',
      readSecret: async () => TOKEN,
      askLine: async () => 'yes',
      spawnSync: (command, arguments_, options) => command === 'powershell.exe'
        ? { status: 2, stdout: '', stderr: '' }
        : spawnSync(command, arguments_, options),
    });
    assert.equal(await main(['install', '--client', 'codex'], failed.runtime), 2);
    assert.match(failed.stderr.value, /did not confirm/);
    assert.doesNotMatch(failed.stdout.value, /stored as/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('uninstall removes files that were created only for Acceptora', async () => {
  const root = createProject();
  try {
    const { runtime, stderr } = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], runtime), 0, stderr.value);
    assert.equal(await main(['uninstall'], runtime), 0, stderr.value);
    assert.equal(existsSync(join(root, 'AGENTS.md')), false);
    assert.equal(existsSync(join(root, '.codex/config.toml')), false);
    assert.equal(existsSync(join(root, '.codex')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('install restores the project when a managed file write fails', async () => {
  const root = createProject({
    'AGENTS.md': 'User instruction.\n',
    '.codex/config.toml': '[other]\nvalue = true\n',
  });
  try {
    let writes = 0;
    const { runtime } = testRuntime(root, {
      atomicWrite: (path, content, write) => {
        writes += 1;
        if (writes === 2) {
          throw new Error('synthetic write failure');
        }
        write(path, content);
      },
    });
    assert.equal(await main(['install', '--client', 'codex'], runtime), 1);
    assert.equal(readFileSync(join(root, 'AGENTS.md'), 'utf8'), 'User instruction.\n');
    assert.equal(readFileSync(join(root, '.codex/config.toml'), 'utf8'), '[other]\nvalue = true\n');
    assert.equal(existsSync(join(root, '.agents/skills/acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
    assert.equal(existsSync(join(root, '.acceptora')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('update and uninstall restore the complete installation when a write fails', async (context) => {
  for (const command of ['update', 'uninstall']) {
    await context.test(command, async () => {
      const root = createProject({
        'CLAUDE.md': 'User instruction.\n',
        '.mcp.json': `${JSON.stringify({ setting: true, mcpServers: { other: { url: 'https://example.test/mcp' } } }, null, 2)}\n`,
      });
      try {
        const installed = testRuntime(root);
        assert.equal(await main(['install', '--client', 'claude-code'], installed.runtime), 0, installed.stderr.value);
        const before = allFileText(root);
        let writes = 0;
        const mutation = testRuntime(root, {
          atomicWrite: (path, content, write) => {
            writes += 1;
            if (writes === 2) {
              throw new Error('synthetic write failure');
            }
            write(path, content);
          },
        });
        assert.equal(await main([command], mutation.runtime), 1);
        assert.equal(allFileText(root), before);
        const diagnosis = testRuntime(root);
        assert.equal(await main(['doctor'], diagnosis.runtime), 0, diagnosis.stderr.value);
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test('different worktrees remain bound to different derived project variables', async () => {
  const secondProjectUlid = '01BX5ZZKBKACTAV9WEVGEMMVRZ';
  const secondCredentialUlid = '01BX5ZZKBKACTAV9WEVGEMMVRX';
  const secondProjectId = `proj_${secondProjectUlid}`;
  const secondTokenEnv = `ACCEPTORA_AGENT_TOKEN_PROJ_${secondProjectUlid}`;
  const secondToken = `avt_${secondCredentialUlid}_${'B'.repeat(48)}`;
  const first = createProject();
  const second = createProject();
  try {
    const firstRuntime = testRuntime(first).runtime;
    const secondRuntime = testRuntime(second, {
      env: { [secondTokenEnv]: secondToken },
      fetch: (url) => String(url).endsWith('/api/v1/integrations/project')
        ? Promise.resolve(projectResponse(secondProjectId))
        : Promise.reject(new Error(`Unexpected URL: ${url}`)),
    }).runtime;
    assert.equal(await main(['install', '--client', 'codex'], firstRuntime), 0);
    assert.equal(await main(['install', '--client', 'codex'], secondRuntime), 0);

    const firstConfig = JSON.parse(readFileSync(join(first, '.acceptora/config.json'), 'utf8'));
    const secondConfig = JSON.parse(readFileSync(join(second, '.acceptora/config.json'), 'utf8'));
    assert.equal(firstConfig.token_env, TOKEN_ENV);
    assert.equal(secondConfig.token_env, secondTokenEnv);
    assert.notEqual(firstConfig.project_id, secondConfig.project_id);
  } finally {
    rmSync(first, { recursive: true, force: true });
    rmSync(second, { recursive: true, force: true });
  }
});
