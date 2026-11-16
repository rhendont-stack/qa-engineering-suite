---
tags: [trigger, en]
runs: 3
max_turns: 3
timeout_seconds: 180
allowed_tools: [Skill, Read, Glob, Grep]
---
Are we doing N+1 queries anywhere in the data-access code? Show me the statements one request actually issues.
