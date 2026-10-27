path = r'docs/TECHNICAL_DOCUMENTATION.md'
s = open(path, encoding='utf-8').read()
old = 'the installed `perunchat` OpenCode agent now instruct the model to answer\n  questions using the provided participant roster and conversation context, and\n  to avoid generic greetings when a question was asked. The agent file is\n  overwritten on bridge start whenever the embedded content changes.'
if old in s:
    tail = s.split(old)[-1][:400]
    if 'health check' not in tail:
        new = old + '\n* **Agent auto-install and health check.** The `perunchat` and `perun-shared` agents are auto-installed/updated into both `agents/` and `agent/` OpenCode config directories (respecting `OPENCODE_CONFIG_DIR`/`XDG_CONFIG_HOME`). Added `GET/POST /api/health/check` and a Dashboard "Health check & fix" button.'
        open(path, 'w', encoding='utf-8').write(s.replace(old, new))
        print('updated')
    else:
        print('skip')
else:
    print('notfound')
