# Contributing

Acceptora Agent Skill supports Python 3.11 or newer and Git.

Keep changes small and project-local. The package repository is independent from the Acceptora application repository; do not mix their source, dependencies, or release workflows.

Test executable behavior only. Do not add tests that lock Markdown wording or other documentation text.

Run the focused test for the behavior you changed. Before a pull request, run:

```text
python -B -m unittest discover -s tests -p "test_*.py"
python -B -m compileall -q scripts tests
git diff --check
```

Do not add hooks, external runtimes, offline queues, compatibility layers, dependencies, shared user configuration, or generalized client registries without a demonstrated requirement. Keep API/MCP contract changes synchronized with the independently released application and package.

Never include credentials, private source, personal data, customer data, or production payloads in fixtures, logs, errors, or examples. Follow [SECURITY.md](SECURITY.md) for vulnerability reports.
