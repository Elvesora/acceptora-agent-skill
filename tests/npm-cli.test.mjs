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
const TOKEN_ENV = 'ACCEPTORA_PROJECT_TOKEN';
const TOKEN_FILE = '.acceptora-env';
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
      env: {},
      platform: 'linux',
      stdout,
      stderr,
      fetch: fakeFetch,
      readSecret: async () => TOKEN,
      ...overrides,
    },
    stdout,
    stderr,
  };
}

function allFileText(root, excluded = new Set()) {
  const values = [];
  const visit = (directory) => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      const metadata = lstatSync(path);
      const relativePath = relative(root, path);
      if (excluded.has(relativePath)) {
        continue;
      }
      if (metadata.isDirectory()) {
        if (name !== '.git') {
          visit(path);
        }
      } else if (metadata.isFile()) {
        values.push(`${relativePath}\n${readFileSync(path, 'utf8')}`);
      }
    }
  };
  visit(root);
  return values.join('\n');
}

function nonCredentialFileText(root) {
  return allFileText(root, new Set([TOKEN_FILE]));
}

test('install stores the key only in the project credential file and completes for each client', async (context) => {
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
        assert.equal(config.installed_version, '1.0.2');
        const manifest = JSON.parse(readFileSync(join(root, '.acceptora/install-manifest.json'), 'utf8'));
        assert.equal(manifest.client, client);
        assert.equal(manifest.package_version, '1.0.2');
        assert.equal(Object.keys(manifest.payload).length, 5);
        assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
        assert.match(stdout.value, /Add \/\.acceptora-env to \.gitignore/);
        assert.doesNotMatch(`${stdout.value}${stderr.value}${nonCredentialFileText(root)}`, new RegExp(TOKEN));

        if (client === 'codex') {
          const mcp = readFileSync(join(root, paths[2]), 'utf8');
          assert.match(mcp, /http_headers_helper = 'node "\.agents\/skills\/acceptora\/scripts\/mcp-headers\.mjs"'/);
          assert.doesNotMatch(mcp, /bearer_token_env_var/);
        }
        if (client === 'claude-code') {
          const mcp = JSON.parse(readFileSync(join(root, paths[2]), 'utf8'));
          assert.equal(mcp.mcpServers.acceptora.headersHelper, 'node ".claude/skills/acceptora/scripts/mcp-headers.mjs"');
          assert.equal(mcp.mcpServers.acceptora.headers, undefined);
        }
        if (client === 'gemini-cli') {
          const mcp = JSON.parse(readFileSync(join(root, paths[2]), 'utf8'));
          assert.equal(mcp.mcpServers.acceptora.headers.Authorization, `Bearer \${${TOKEN_ENV}}`);
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
    const { runtime, stdout, stderr } = testRuntime(root, { env: {}, stdin, readSecret: null });
    stdout.isTTY = true;

    const installation = main(['install', '--client', 'codex'], runtime);
    queueMicrotask(() => stdin.emit('data', `${TOKEN}\r`));
    const status = await installation;

    assert.equal(status, 0, stderr.value);
    assert.match(stdout.value, /Acceptora project key \(hidden\): /);
    assert.match(stdout.value, new RegExp(`\\*{${TOKEN.length}}`));
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
    assert.equal(stdin.isRaw, false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('missing credential creates .acceptora-env, leaves app .env untouched, and installs in the same run', async () => {
  const root = createProject({ '.env': 'APP_SETTING=value\n' });
  try {
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      readSecret: async () => TOKEN,
    });
    const status = await main(['install', '--client', 'codex'], runtime);

    assert.equal(status, 0, stderr.value);
    assert.match(stdout.value, /Do not run install again/);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(readFileSync(join(root, '.env'), 'utf8'), 'APP_SETTING=value\n');
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
    assert.equal(existsSync(join(root, '.acceptora/config.json')), true);
    assert.equal(existsSync(join(root, '.agents/skills/acceptora/SKILL.md')), true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('install reuses an existing project key without prompting and preserves comments', async () => {
  const root = createProject({ [TOKEN_FILE]: `# Acceptora project\r\n${TOKEN_ENV}=${TOKEN}\r\n` });
  try {
    let authorization = null;
    const { runtime, stderr } = testRuntime(root, {
      readSecret: async () => {
        throw new Error('credential prompt was not expected');
      },
      fetch: (url, options) => {
        authorization = options.headers.Authorization;
        return fakeFetch(url, options);
      },
    });

    assert.equal(await main(['install', '--client', 'codex'], runtime), 0, stderr.value);
    assert.equal(authorization, `Bearer ${TOKEN}`);
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `# Acceptora project\r\n${TOKEN_ENV}=${TOKEN}\r\n`);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('installed MCP header helper reads the key from its own project root', async () => {
  const root = createProject();
  try {
    const installed = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], installed.runtime), 0, installed.stderr.value);

    const helper = join(root, '.agents/skills/acceptora/scripts/mcp-headers.mjs');
    const result = spawnSync(process.execPath, [helper], {
      cwd: tmpdir(),
      encoding: 'utf8',
      env: {},
    });

    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), { Authorization: `Bearer ${TOKEN}` });

    writeFileSync(join(root, TOKEN_FILE), `${TOKEN_ENV}=${TOKEN}\n${TOKEN_ENV}=${TOKEN}\n`, 'utf8');
    const rejected = spawnSync(process.execPath, [helper], {
      cwd: tmpdir(),
      encoding: 'utf8',
      env: {},
    });
    assert.equal(rejected.status, 1);
    assert.equal(rejected.stdout, '');
    assert.doesNotMatch(rejected.stderr, new RegExp(TOKEN));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('tracked .acceptora-env is rejected before installation writes', async () => {
  const root = createProject({ [TOKEN_FILE]: `${TOKEN_ENV}=${TOKEN}\n` });
  try {
    const tracked = spawnSync('git', ['-C', root, 'add', TOKEN_FILE], { encoding: 'utf8' });
    assert.equal(tracked.status, 0, tracked.stderr);
    const { runtime, stdout, stderr } = testRuntime(root, {
      env: {},
      readSecret: async () => TOKEN,
    });
    const status = await main(['install', '--client', 'codex'], runtime);

    assert.equal(status, 2, stderr.value);
    assert.match(stderr.value, /tracked by Git/);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
    assert.equal(existsSync(join(root, '.acceptora')), false);
    assert.equal(existsSync(join(root, '.agents')), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('duplicate project key entries are rejected before network or installation writes', async () => {
  const root = createProject({ [TOKEN_FILE]: `${TOKEN_ENV}=${TOKEN}\n${TOKEN_ENV}=${TOKEN}\n` });
  try {
    const { runtime, stdout, stderr } = testRuntime(root, {
      fetch: () => {
        throw new Error('duplicate key file reached network');
      },
    });
    assert.equal(await main(['install', '--client', 'codex'], runtime), 2);
    assert.match(stderr.value, /duplicate/);
    assert.doesNotMatch(`${stdout.value}${stderr.value}`, new RegExp(TOKEN));
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
    assert.equal(existsSync(join(root, TOKEN_FILE)), false);
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

test('update migrates a 1.0.1 project variable into .acceptora-env without another install', async (context) => {
  const legacyTokenEnv = `ACCEPTORA_AGENT_TOKEN_${PROJECT_ID.toUpperCase()}`;
  for (const client of ['codex', 'claude-code', 'gemini-cli']) {
    await context.test(client, async () => {
      const root = createProject();
      try {
        const installed = testRuntime(root);
        assert.equal(await main(['install', '--client', client], installed.runtime), 0, installed.stderr.value);

        const configPath = join(root, '.acceptora/config.json');
        const config = JSON.parse(readFileSync(configPath, 'utf8'));
        config.installed_version = '1.0.1';
        config.token_env = legacyTokenEnv;
        writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');

        const manifestPath = join(root, '.acceptora/install-manifest.json');
        const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
        manifest.package_version = '1.0.1';
        writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');

        if (client === 'codex') {
          writeFileSync(
            join(root, '.codex/config.toml'),
            `# acceptora-mcp:start\n[mcp_servers.acceptora]\nurl = "https://www.acceptora.com/mcp"\nbearer_token_env_var = "${legacyTokenEnv}"\n# acceptora-mcp:end\n`,
            'utf8',
          );
        } else {
          const mcpPath = client === 'claude-code' ? join(root, '.mcp.json') : join(root, '.gemini/settings.json');
          const mcp = JSON.parse(readFileSync(mcpPath, 'utf8'));
          mcp.mcpServers.acceptora = {
            type: 'http',
            url: 'https://www.acceptora.com/mcp',
            headers: { Authorization: `Bearer \${${legacyTokenEnv}}` },
          };
          if (client === 'gemini-cli') {
            mcp.security.environmentVariableRedaction.allowed = [legacyTokenEnv];
          }
          writeFileSync(mcpPath, `${JSON.stringify(mcp, null, 2)}\n`, 'utf8');
        }
        rmSync(join(root, TOKEN_FILE));

        const migrated = testRuntime(root, {
          env: { [legacyTokenEnv]: TOKEN },
          readSecret: async () => {
            throw new Error('legacy environment credential should be reused');
          },
        });
        assert.equal(await main(['update'], migrated.runtime), 0, migrated.stderr.value);

        const migratedConfig = JSON.parse(readFileSync(configPath, 'utf8'));
        assert.equal(migratedConfig.installed_version, '1.0.2');
        assert.equal(migratedConfig.token_env, TOKEN_ENV);
        assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });
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

test('doctor is read-only when the project credential file is missing', async () => {
  const root = createProject();
  try {
    const installed = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], installed.runtime), 0, installed.stderr.value);
    rmSync(join(root, TOKEN_FILE));

    let prompted = false;
    const diagnosis = testRuntime(root, {
      env: {},
      readSecret: async () => {
        prompted = true;
        return TOKEN;
      },
    });
    assert.equal(await main(['doctor'], diagnosis.runtime), 2);
    assert.match(diagnosis.stderr.value, /\.acceptora-env/);
    assert.equal(prompted, false);
    assert.doesNotMatch(`${diagnosis.stdout.value}${diagnosis.stderr.value}`, new RegExp(TOKEN));

    const recovery = testRuntime(root, {
      readSecret: async () => {
        prompted = true;
        return TOKEN;
      },
    });
    assert.equal(await main(['update'], recovery.runtime), 0, recovery.stderr.value);
    assert.equal(prompted, true);
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
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
      env: {
        [TOKEN_ENV]: 'must-not-reach-git',
        ACCEPTORA_AGENT_TOKEN: 'legacy-must-not-reach-git',
      },
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

test('uninstall removes files that were created only for Acceptora', async () => {
  const root = createProject();
  try {
    const { runtime, stderr } = testRuntime(root);
    assert.equal(await main(['install', '--client', 'codex'], runtime), 0, stderr.value);
    assert.equal(await main(['uninstall'], runtime), 0, stderr.value);
    assert.equal(existsSync(join(root, 'AGENTS.md')), false);
    assert.equal(existsSync(join(root, '.codex/config.toml')), false);
    assert.equal(existsSync(join(root, '.codex')), false);
    assert.equal(readFileSync(join(root, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
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
    assert.equal(existsSync(join(root, TOKEN_FILE)), false);
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

test('different worktrees keep different keys in their own .acceptora-env files', async () => {
  const secondProjectUlid = '01BX5ZZKBKACTAV9WEVGEMMVRZ';
  const secondCredentialUlid = '01BX5ZZKBKACTAV9WEVGEMMVRX';
  const secondProjectId = `proj_${secondProjectUlid}`;
  const secondToken = `avt_${secondCredentialUlid}_${'B'.repeat(48)}`;
  const first = createProject();
  const second = createProject();
  try {
    const firstRuntime = testRuntime(first).runtime;
    const secondRuntime = testRuntime(second, {
      readSecret: async () => secondToken,
      fetch: (url) => String(url).endsWith('/api/v1/integrations/project')
        ? Promise.resolve(projectResponse(secondProjectId))
        : Promise.reject(new Error(`Unexpected URL: ${url}`)),
    }).runtime;
    assert.equal(await main(['install', '--client', 'codex'], firstRuntime), 0);
    assert.equal(await main(['install', '--client', 'codex'], secondRuntime), 0);

    const firstConfig = JSON.parse(readFileSync(join(first, '.acceptora/config.json'), 'utf8'));
    const secondConfig = JSON.parse(readFileSync(join(second, '.acceptora/config.json'), 'utf8'));
    assert.equal(firstConfig.token_env, TOKEN_ENV);
    assert.equal(secondConfig.token_env, TOKEN_ENV);
    assert.notEqual(firstConfig.project_id, secondConfig.project_id);
    assert.equal(readFileSync(join(first, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${TOKEN}\n`);
    assert.equal(readFileSync(join(second, TOKEN_FILE), 'utf8'), `${TOKEN_ENV}=${secondToken}\n`);
  } finally {
    rmSync(first, { recursive: true, force: true });
    rmSync(second, { recursive: true, force: true });
  }
});
