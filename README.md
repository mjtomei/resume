# tmux-resume

Checkpoint Linux tmux workspaces, then reopen their named sessions, windows,
panes, agent conversations, and saved editor files after a reboot.

Requires Linux (`/proc`), Python 3.10+, tmux, and the programs being restored.
No Python packages are needed. Command autofill requires Bash with Readline;
other shells display the prepared command for copying.

Run the command from a terminal that can access your tmux sockets and process
metadata. An agent's restricted sandbox may deny that access even when the
agent runs under the same user account.

## Install

From the source directory or an unpacked release archive:

```sh
./install.sh                         # command plus Vim plugin
./install.sh --emacs-plugin          # also install the Emacs helper
./install.sh --without-vim-plugin    # command only
./install.sh --prefix /your/prefix
```

The default installation copies the bundle into `~/.local/share/tmux-resume/`
and links `~/.local/bin/tmux-resume` to it. Put `~/.local/bin` on `PATH`. The
original source directory can then be moved or removed. Changed existing files
are backed up beside their replacements. Editor plugin paths are independent
of `--prefix`; the separate installers below accept `--directory`.
Installation creates `~/.config/tmux-resume/config.json` with the default history
limit if no configuration exists, respecting `XDG_CONFIG_HOME`.

Upgrading from the previous `tmux-travel` name migrates the installed bundle,
checkpoint history, restore receipts and configuration to `tmux-resume` paths.
The latest-checkpoint pointer is updated. Commands, logs, helpers and services
use `tmux-resume`; old-name aliases and loaders are not installed. If both old
and new directories already exist, installation refuses to merge or overwrite
them. Update editor configuration references to `plugin/tmux_resume.vim` and
`tmux-resume.el`, and restart editors that still have the old helper loaded.
Already-running launchers keep their old process names; `check` can report
them as extra jobs until those panes are restarted with the new launcher.

## Use

```sh
tmux-resume servers              # list live servers, sockets, and sessions
tmux-resume save                 # checkpoint all your servers; keep them running
tmux-resume check                # checkpoint and report closing blockers
tmux-resume save --close         # checkpoint twice, then close if checks pass
tmux-resume save --force-close   # checkpoint twice, then close despite pane blockers
tmux-resume restore --dry-run    # inspect planned commands and duplicate guards
tmux-resume restore              # restore to each server's original socket
tmux-resume restore --skip-existing # restore missing sessions; leave existing ones alone
tmux-resume restore --skip-codex-update # bypass the update check and suppress Codex update dialogs
tmux-resume show                 # summarize the latest checkpoint
tmux-resume history              # list older checkpoints and their restore paths
```

`save` succeeds even when panes have blockers. `save --close` refuses to close
anything if any selected pane has unsaved buffers, unknown editor state, an
unidentified agent conversation, or an unsupported running job. `check` returns
exit status 2 when blockers are present. Neither command writes your text files.

`save --force-close` implies closing; you do not need to add `--close`. It
bypasses pane blockers, including unsaved buffers, unknown editor state,
unidentified conversations, and unsupported running jobs. **Unsaved text may
be lost and running jobs are terminated.** It prints the overridden blockers
and records them in the final checkpoint. It still requires both checkpoints
to succeed and the selected server/session/pane structure to remain unchanged;
server and session identities are checked again before closing. Session/server
selectors still apply, including PM workspace selection. Restore behavior and
duplicate protection are unchanged: saved files reopen, and unsupported or
unidentified commands are prefilled for review without Enter.

If you save with blockers, shut down manually, and restore, supported agent and
editor sessions reopen. Other commands appear at a Bash prompt **without Enter
being sent**. Edit the command, press Enter to run it, or Ctrl-C to discard it.
Unsaved editor text is not in the checkpoint; only the files on disk reopen.
Use the editor's normal swap/autosave recovery for text lost during shutdown.
Unsaved text in an agent's prompt box is also outside the checkpoint.

### Shutdown checkpoints with systemd

After installing the command for your account, install and enable its system
service for your account:

```sh
sudo ~/.local/bin/tmux-resume install-systemd --user "$(id -un)"
```

This creates `tmux-resume-shutdown-UID.service` and an ordering drop-in under
`/etc/systemd/system/session-.scope.d/`. The service remains active after boot
and runs `tmux-resume save --if-running` when it stops during shutdown or reboot.
The checkpoint command runs as the selected user and includes all their servers,
including PM. It does not close sessions or write editor text files. Pane
blockers are recorded without failing the save or blocking shutdown. If no tmux
servers remain (for example, after a manual `save --close`), it leaves the
previous checkpoint and `latest` untouched. There is no automatic restore on boot.

The system service and login-scope drop-in ensure the checkpoint stops before
the user manager and login scopes. A user service alone cannot provide that
ordering. The drop-in adds an ordering dependency to all login scopes; it does
not start services or stop sessions itself. This uses systemd's
[reverse ordering at shutdown](https://github.com/systemd/systemd/blob/v255/man/systemd.unit.xml).
If tmux is managed by an additional system service outside the user manager,
add that unit to the checkpoint service's `After=` list as well.

For UID 1000, inspect or test the service without shutting down:

```sh
systemctl status tmux-resume-shutdown-1000.service
sudo systemctl restart tmux-resume-shutdown-1000.service
journalctl -u tmux-resume-shutdown-1000.service -n 100
tmux-resume history
```

Restarting performs a checkpoint and arms the service again. Use
`sudo systemctl disable --now tmux-resume-shutdown-1000.service` to disable it;
stopping it also performs one final checkpoint. `install-systemd --directory
/path/to/review --user "$(id -un)"` generates both files without root or activation.
Use `--prefix /absolute/prefix` for a nondefault command installation, or
`--no-enable` to install unit files without enabling them.

The service defaults to the user's `~/.local/state` and `~/.config`; for custom
XDG paths, set `Environment=XDG_STATE_HOME=...` and
`Environment=XDG_CONFIG_HOME=...` with `sudo systemctl edit UNIT`. Add a custom
state mount to `RequiresMountsFor=` if needed. `TimeoutStopSec=300` gives the
checkpoint up to five minutes; it can be overridden there too. A failed or
timed-out checkpoint is reported in the journal, and shutdown continues.
This covers orderly shutdown, not power loss, crashes, or forced shutdown that
bypasses systemd. Unsaved editor text still requires the editor's recovery files.

### All servers by default

`save` and `check` discover all running tmux servers owned by your user, including
the default server, named servers (`tmux -L NAME`), and custom sockets
(`tmux -S PATH`). This also applies when you invoke the command inside tmux.
Discovery checks the usual socket directories and the listening sockets of your
tmux server processes in `/proc`; it does not depend on panes retaining `TMUX`.
Stale sockets and servers without sessions are skipped. An identified live
server that cannot be inspected causes an error instead of an incomplete save.

One checkpoint contains separate per-server snapshots. `restore` sends each
snapshot back to its **original socket path**, creating missing socket parent
directories. A server saved from `tmux -L work` can be reached with `tmux -L work`
again after restoration, using the same `TMUX_TMPDIR` if you customized it.
Custom sockets retain their complete paths. Restore prints an explicit attach
command for each server. PM workspaces also receive their saved socket through
`PM_TMUX_SOCKET` and bootstrap through `pm session` as before.

Existing single-server checkpoints remain supported and now default to their
recorded source socket too. Take a new checkpoint to include additional servers;
older checkpoints cannot recover servers they never captured. Tmux configuration
files and arbitrary server options are not backed up by this tool.

### Target a session or tmux server

`--session` takes an exact saved/live tmux name, depending on the command. Repeat
it to select several sessions, including sessions on different servers. If a name
occurs on more than one selected server, the command refuses and asks you to
narrow the scope with `--server` or `--socket`. Without selectors, all sessions
on all discovered/saved servers are included. Other sessions are not closed or
recreated.

```sh
tmux-resume save --session coherence
tmux-resume check --session coherence
tmux-resume restore --session coherence --dry-run
tmux-resume restore --session coherence
tmux-resume show /path/to/snapshot --session coherence

tmux-resume save --session coherence --session cosmic --output /path/to/new/snapshot
tmux-resume --server work save
tmux-resume --server work restore /path/to/snapshot --session coding --dry-run
tmux-resume --server work restore /path/to/snapshot --session coding
tmux-resume --socket /path/to/tmux.sock save --session work
tmux-resume --socket /path/to/tmux.sock restore /path/to/snapshot --session work
```

`--server` (also `-L`) and `--socket` are mutually exclusive global options,
before the subcommand. `--server NAME` selects a named server: saving uses the
normal `TMUX_TMPDIR` or `/tmp` location, while restoring selects the saved name
and uses its recorded socket. `--server default` selects the default server.
If the same server name exists under multiple saved socket directories, use its
full `--socket` path to disambiguate.

On restore, `--socket PATH` selects that server from a multi-server checkpoint.
If PATH is not a saved socket and only one server remains after session
selection, it overrides that server's destination for manual testing. It refuses
to combine multiple saved servers into one destination. To move a single named
server to a different socket, use its `servers/NNN` subdirectory as the snapshot.

Linked windows remain shared wherever both selected sessions include them.
Selecting a session does not include unrelated sessions in its ordinary tmux
group; closing one group member may leave its windows running through another.

### Already-running protection

By default, restore refuses the entire selected operation before creating panes if:

- Any requested session name already exists on the destination server.
- A PM workspace or any of its attachment sessions is already there.
- The same checkpoint was previously restored into a session that is still
  running, even if renamed or on another socket. Local restore receipts track
  the server identity and session ID; stale records expire automatically.
- A matching Claude/Codex conversation is found in another live tmux pane owned
  by your user, including on another socket. An unidentified agent of the same
  family in the same directory also blocks restore when a duplicate is possible.

To restore the missing sessions from a checkpoint while keeping sessions that
are already running, use:

```sh
tmux-resume restore --skip-existing --dry-run
tmux-resume restore --skip-existing
tmux-resume restore /path/to/checkpoint --skip-existing --session work --session notes
```

`--skip-existing` reports and skips names already present on each destination
server. It also skips a workspace that restore receipts identify as already
restored, even if renamed or running on another socket. If a PM base session or
any attachment is present, the whole PM workspace is skipped. If everything is
already present, the command succeeds without changing any sessions.

Only the remaining sessions undergo remote-server, Codex update, and launch preflight checks.
Other protections still apply: a missing session cannot start an agent
conversation that is already running elsewhere, and a failed check prevents all
remaining sessions from being created. This flag does not fill in missing
windows or panes inside an existing session, or merge its windows with a newly
restored session. It works with server/session selectors and `--dry-run`.

All selected servers pass remote Codex readiness, duplicate, directory,
executable, and editor-file checks before any workspace is created. Structures
are built before agents are launched; a build failure rolls back the newly
created structures across servers.
If a program launch fails after launching has begun, restored sessions remain
available for inspection. `save --close` checks buffers and identities across all
selected servers and refuses to close any if a blocker or topology change is
detected. `--force-close` bypasses pane blockers while retaining the topology
and identity checks. Closing separate tmux servers is sequential, so an external shutdown or
concurrent change during the final close can still interrupt that operation.

Restores by this tool are serialized to prevent simultaneous launches. There is
no force-overwrite option. Attach to the existing session to continue working,
or stop it before restoring. A plain `save` leaves the original running, so an
immediate `restore` is expected to refuse. `--dry-run` reports the conflict
without creating sessions.

The duplicate-agent check uses local process and conversation metadata. It does
not query other machines or headless remote agent servers, and cannot identify every
untracked conversation. Restore receipts live in the same state directory; use
the same `XDG_STATE_HOME` across invocations for this additional protection.

For a small manual test on a separate server:

```sh
resume_test=$(mktemp -d)
tmux -S "$resume_test/tmux.sock" -f /dev/null new-session -d -s resume-test \
  '/bin/bash --noprofile --norc'
tmux-resume --socket "$resume_test/tmux.sock" save --session resume-test \
  --output "$resume_test/checkpoint" --close
tmux-resume --socket "$resume_test/tmux.sock" restore "$resume_test/checkpoint" --dry-run
# Wait for the detached close to finish if the preview still reports a collision.
tmux-resume --socket "$resume_test/tmux.sock" restore "$resume_test/checkpoint"
tmux -S "$resume_test/tmux.sock" attach -t resume-test
# Repeating restore now refuses. Later, remove only this test server:
tmux -S "$resume_test/tmux.sock" kill-server
```

### PM workspaces

PM is recognized by its `pm-...` session and `pm _tui` dashboard. Restore runs
**`pm session` in the original tmux session creation directory**, waits for PM
to create the workspace, and detaches only the temporary bootstrap client.
It keeps PM's new dashboard and rebuilds the saved agent/editor panes in that
workspace. It does not execute the old dashboard Python command.

Saved attachment clones such as `~1` and `~2` are not reconstructed. PM may
create its own fresh `~1` as part of normal startup. Selecting either the base
name or an attachment name targets the whole PM workspace:

```sh
tmux-resume save --session pm-project_manager-c5a1006b
tmux-resume restore --session pm-project_manager-c5a1006b --dry-run
```

PM must be on `PATH`, support `pm session name`, and honor `PM_TMUX_SOCKET` for
alternate sockets. The computed workspace name must match the checkpoint.
Startup output is kept in `pm-*.log` inside the snapshot. PM's own hooks and
layout policies still apply after startup. Older snapshots use the captured
dashboard directory if they do not contain tmux's original session path.

## What is saved

Snapshots live under `~/.local/state/tmux-resume/`, or
`$XDG_STATE_HOME/tmux-resume/`. New checkpoints contain a version-2 `snapshot.json`
manifest listing servers and sockets. Each `servers/NNN/` subdirectory contains
an independently restorable single-server snapshot:

- `snapshot.json`: process arguments, conversation IDs, directories, tmux
  structure, editor buffer status, warnings, and prepared restore commands.
- Numbered `.txt` files: pane scrollback for reference.
- `vim-*/session.vim` and `emacs-*/session.el`: editor reopening instructions.

`latest` points to the most recent checkpoint, **including a scoped save or
check**. Pass a snapshot path explicitly when testing several checkpoints.
`save --close` takes a second checkpoint and points `latest` at its `close-check`
subdirectory. Detached cleanup writes `close.log` there and can finish even
when it closes the pane that invoked the command.

### Checkpoint history and retention

Older versions already kept every checkpoint directory; `latest` is just a
symlink. Automatic saves and checks now retain the **newest 100 completed
checkpoints** by default. A save and its nested `close-check` count as one.
List them newest first, then use the printed path to inspect or restore one:

```sh
tmux-resume history
tmux-resume show /path/printed/by/history
tmux-resume restore /path/printed/by/history --dry-run
tmux-resume restore /path/printed/by/history
```

Configure the limit in `~/.config/tmux-resume/config.json` (or
`$XDG_CONFIG_HOME/tmux-resume/config.json`):

```json
{"keep_checkpoints": 100}
```

Use `0` to retain everything. `save --keep 25` or `check --keep 25` overrides the
configuration for that invocation; the shutdown service uses the same config.
Pruning runs after a completed save/check, not merely when viewing history or
installing an update. Existing completed automatic snapshots participate too.

Retention only removes recognized automatic checkpoint directories directly
under the state directory. Explicit `--output` exports, incomplete snapshots,
and symlinked directories are excluded. The latest checkpoint and older
checkpoints still used by live restores or pending close commands are protected,
so the count can temporarily exceed the configured limit. Restores and pruning
are coordinated to keep editor files and launcher metadata available.

Restore preserves names, window indices, linked windows, ordinary tmux group
relationships, splits, active windows/panes, zoom, and working directories.
Automatic window renaming is disabled to retain saved names. Scrollback stays
in text files; it is not injected into interactive programs.

### Agent sessions and command options

Supports `claude`, `claude-mixed`, `codex`, and expanded `codex-rc` invocations.
Arguments come from the running process, retaining argument boundaries and
model, effort, permission, sandbox, search, configuration, and remote options.
Shell aliases are already expanded at that point. `claude-mixed` is recognized
from its local router settings and environment, and restored through the wrapper.

Remote Codex tasks are an exception for permission options: the native CLI
rejects permission overrides when resuming them. Restore uses the task's stored
permission policy and omits those overrides from the resume command, including
sandbox, approval, extra-directory, and permission-related configuration flags.
The original flags remain in the checkpoint, and a warning explains the change.
Other options, including remote connection, model, effort, and search, are kept.
Local Codex resumes retain their permission flags.

For remote tasks, this is the server's **current** stored policy, not a frozen
copy of the policy at checkpoint time. If another client changes the same
conversation's permissions between save and restore, tmux-resume does not
change them back. An approval policy of `never` is separate from sandbox access;
it does not imply unrestricted filesystem or network access.

Before restoring any tmux or PM sessions, `restore` checks every remote Codex
endpoint needed by the selected sessions. If one is unavailable, restore exits
with an explanation and creates no sessions or panes. For the usual local
remote-control server, run `codex remote-control start`, then rerun the same
`tmux-resume restore` command. Custom `CODEX_HOME`, Unix sockets, and WebSocket
addresses are checked using the saved connection settings. Each connection has
a three-second socket timeout. `restore --dry-run` performs this check too;
`show` can inspect a checkpoint while its remote servers are offline.

Restore also checks the installed Codex executable for each selected Codex
session, including remote sessions. If an update is available, it exits before
creating any tmux or PM sessions. Install it with `codex update` (or your package
manager), then rerun restore. This checks the local client; it does not update or
restart a remote-control server.

The check uses Codex's `version.json` in each saved `CODEX_HOME` (normally
`~/.codex`). Metadata less than 20 hours old is reused. Otherwise, restore queries
the official GitHub stable release endpoint once per Codex home, with a
three-second network timeout. If that fails, it warns and uses any usable cached
version. Without usable metadata, or if the installed version cannot be checked,
restore refuses to proceed. Custom or prerelease version strings require the
explicit bypass below. `--dry-run` performs the same checks; `show` stays offline.

To resume without installing an update or checking update availability:

```sh
tmux-resume restore --skip-codex-update
# Can also be combined with --skip-existing, --session, or --dry-run.
```

After the central check passes, or when using this bypass, automatic Codex
launches append `-c check_for_update_on_startup=false`. This supported
[Codex setting](https://developers.openai.com/codex/config-reference/)
prevents each pane from asking to update, including if update metadata changes
during restore. Prepared retry commands retain it. Saved checkpoint commands
and your global Codex configuration are unchanged. The bypass leaves the remote
connection and duplicate-session protections enabled.

If a server stops after preflight, or another resume error occurs, the pane
displays the program's error and prepares the exact resume command for retry
at the shell prompt, without submitting it.
For remote Codex, an explanation reminds you that the remote-control server
must be running and reachable. Start it yourself (or through your own service),
then press Enter on the prepared command to resume the same conversation.
tmux-resume does not start remote-control servers or retry in a loop. A normal
successful program exit leaves an ordinary shell prompt.

Existing resume/continue/fork selectors are replaced with the identified
conversation ID. Startup prompts, image attachments, and worktree-creation flags
are omitted to avoid repeating submitted work. Original and omitted arguments
remain in the JSON. Unknown CLI options block automatic replay and closing;
the original command is prefilled for review instead.

Claude IDs come from the process session registry or open task directories.
Codex IDs come from unique assistant transcript matches, open rollout files,
or PID-scoped TUI history logs. It does not choose the newest conversation merely
because it shares the directory. These Codex methods are inferred evidence;
review `restore --dry-run`, especially after switching threads. Missing or
ambiguous evidence blocks closing. Each agent's `evidence` field describes the
identification method.

Relevant routing/model/configuration environment variables are saved. The full
environment and authentication files are not copied. Provide other credentials
and custom environment through your normal login setup. Keep the original
conversation stores, project files, configuration, and wrappers available.

This checkpoints workspace structure, not process memory, in-progress tool jobs,
or a remote app server. Snapshots contain private arguments and scrollback and
are created with private permissions. Treat snapshot files and generated editor
scripts as trusted local data; inspect a checkpoint before executing one received
from someone else.

## Vim

The default installer adds `~/.vim/plugin/tmux_resume.vim`. It loads in future
regular Vim instances. In each already-running Vim, run:

```vim
:runtime plugin/tmux_resume.vim
```

Separate installation, including a custom Vim/Neovim plugin directory:

```sh
tmux-resume install-vim-plugin
tmux-resume install-vim-plugin --directory ~/.config/nvim/plugin
```

The helper uses timers and JSON; it does not require Vim `+clientserver`.
It reports loaded/listed buffers and writes a Vim session containing named files,
tabs, splits, folds, and positions. **It never writes your text files.** Modified
buffers, including unnamed ones, are reported as `UNSAVED`. Save named buffers
with `:wall`, or an unnamed buffer with `:saveas /path/to/file`, then checkpoint
again. Restore reopens the saved session using the saved files on disk.
Restored Vim instances also load the bundled helper so they can be checkpointed
again, even if it was loaded manually in the original editor.

After a forced close or crash, Vim may display its normal swap-recovery prompt.
Choose whether to recover the unsaved text or reopen the disk version; tmux-resume
does not delete swap files or make that choice for you.

If the helper is absent or unresponsive, the command warns and uses a separate
clean Vim to read headers of swap files open by that editor. Dirty headers with
a matching editor PID are reported as unsaved evidence. Headers can lag, and
some buffers have no swap, so even clean results **leave state unknown and
block automatic closing**. Swap paths/results are kept in the snapshot for
recovery. On restore, the original Vim command is prefilled without executing it.

Vim terminal buffers and multiple editor processes in one pane also block closing.
Other unsupported editors receive the same conservative command-prefill behavior.

## Emacs

Install and load the helper for standalone terminal Emacs:

```sh
tmux-resume install-emacs-plugin
```

Add to your Emacs initialization file:

```elisp
(load "~/.emacs.d/tmux-resume.el" nil t)
```

For an already-running instance, use `M-x load-file` and select that file.
Alternatively, with an existing Emacs server (`M-x server-start`), tmux-resume
can use `emacsclient --eval` to load the bundled helper and query it on demand.
It checks that a standalone Emacs matches the queried server PID. For
`emacsclient`, it uses that client's explicit socket/server option or default.

The helper reports modified file buffers and nonempty editable buffers such as
`*scratch*`; generated read-only special buffers are excluded from unsaved checks.
It stores named files, positions, and the selected frame's window layout. Restore
starts terminal `emacs -nw --load ...`, reads saved files from disk, and loads the
bundled helper again so the restored editor can be checkpointed. It does
not restore unsaved text, every GUI frame, daemon/client topology, or running
process/terminal buffers. Process buffers block automatic closing.

Without a responding helper or reachable matching server, a warning explains
that buffer state is unknown, closing is blocked, and the original command is
prefilled on restore. A server query checks server buffers, which may include
files opened by other clients. GUI editors outside tmux are not inventoried.

## Development and distribution

```sh
make test
make dist                       # dist/tmux-resume.tar.gz
```

Tests use isolated tmux sockets, Bash, real Vim, and fake Claude/Codex and PM launchers.
Emacs tests use real Emacs/emacsclient when available and otherwise skip. Coverage
includes multi-server discovery, named-socket restoration, cross-server rollback
and closing checks, scoped closing, duplicate refusal across sockets and renames, native PM
bootstrap, layouts, literal argument preservation, autofill without execution,
editor dirty/clean states, and checkpointing reopened editors and agents again.
History tests cover retention, protected checkpoints and shutdown unit generation;
the generated service is checked with `systemd-analyze verify` when available.
`make test` launches no real agents and does not use your PM workspaces.

Validation on September 13, 2026 passed all 56 regression tests with no skips,
plus separate live exercises with native PM, Codex 0.154.0, Vim, Emacs, and
temporary systemd units. These covered server-offline restore refusal,
conversation continuity and permissions, retry after a launch failure,
force-close, editor recovery, history retention, and a fresh archive install.
Shutdown ordering was exercised through a systemd stop transaction without
rebooting the machine. Agent identification depends partly on client internals;
review checkpoint warnings and test again after upgrading your agent clients.

The archive includes source, both editor helpers, the installer, this README,
and tests. It contains no snapshots, personal configuration, or credentials.
