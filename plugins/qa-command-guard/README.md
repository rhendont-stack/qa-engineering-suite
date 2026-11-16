# qa-command-guard

An opt-in plugin for the QA Engineering Suite. It adds one hook that checks every Bash command Claude Code is about to run
with the suite's own command guard (the same rules `qa run` applies), but only while a QA workspace is active.

It is not part of the full `qa-engineering-suite` plugin or of the `qa-core` and `qa-specialists` bundles. Install it
separately, and only if you want the check on commands you approve in the host.

## When it is active

The hook looks for a `.qa/config.json` in the session's working directory and its parents (the kernel's own workspace
lookup). With none, it says nothing and the host's usual permission flow applies. Tools other than Bash are never checked.

## What it decides

The command is classified with the declared activity `static-analysis`, the least one, so every rule that adds an
activity shows up as a grant.

| Guard answer | Hook decision | Reason shown |
| --- | --- | --- |
| `deny` | `deny` | `qa-command-guard: <rule id>: <reason>` |
| `require_grant` | `ask` | names the activities the command needs a grant for |
| `allow` | no decision | the host's own permission flow applies |

A call of the suite's launcher (`python3 <skill-dir>/scripts/qa.py ...`) with no shell metacharacter gets no decision:
`qa run` applies the same guard to what it runs.

## Limits

The guard's time on a command grows faster than the command's length, and the host gives the hook 10 seconds. Measured
end to end on the two test interpreters, commands made of many short units took about 0.9 s at 4 KiB, 2.7 s at 8 KiB and
10 s or more at 16 KiB, so the hook checks commands of at most 4096 bytes (counted in bytes) and asks for a larger one,
naming its size. Where the platform has `SIGALRM`, the check also runs under a 5 second deadline and asks with the reason
`deadline` when it expires. Where it does not (Windows), the size limit alone applies.

If the hook cannot read its input, the workspace config or the guard's policy, or hits any other error, it asks
(`qa-command-guard could not check this command: <error class>`). It always exits 0.

## Install

From the marketplace:

```text
/plugin marketplace add rhendont-stack/qa-engineering-suite
/plugin install qa-command-guard@qa-engineering-suite
```

Python 3.9 or newer, standard library only. The kernel is vendored in `scripts/_kernel/` and kept current by
`tools/vendor_kernel.py`; the hook never imports from anywhere else.
