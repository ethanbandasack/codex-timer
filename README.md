# Codex Timer

Codex Timer is a terminal app for macOS and Ubuntu. It checks Codex reset windows, sends a tiny hello ping, and watches for resets using the Codex CLI and its existing ChatGPT sign-in. It has no Python runtime dependencies.

## Start the app

Install Python 3 and the Codex CLI. On Ubuntu, install the optional desktop notification command:

```sh
sudo apt install python3 libnotify-bin  # Ubuntu
```

From this folder, run:

```sh
bin/codex-timer
```

You can also run `./start-codex-timer`. On macOS, double-click `Start Codex Timer.command` to open the full-screen app in Terminal. The UI refreshes usage data every minute. macOS notifications use `osascript`; Ubuntu notifications use `notify-send`. If a desktop notification command is unavailable or no desktop session is active, reset alerts fall back to a terminal bell.

Keyboard controls:

- `P` sends a one-word hello using `gpt-5.6-luna` at low effort when available. If the installed Codex CLI does not support it, the app uses the CLI's advertised default model at low effort, then closes the in-memory chat.
- Every planned band is an automatic ping. At its scheduled time, the app sends the same one-word hello as `P` and closes its temporary chat. Keep the terminal app open; bands missed while it was closed are skipped. Each band is attempted at most once. The reset row itself is not a ping.
- `R` refreshes usage immediately.
- `S` opens the programming menu, anchored to Codex's reported 5-hour reset. `+` adds a band after the previous row (default `05:01`, giving `08:01` then `13:02` for a `03:00` reset); `↑`/`↓` select rows; `←`/`→` shift the selected row and later rows by five minutes; `E` edits the selected local date and time; `I` changes the band interval and respaces existing bands; `X` deletes one band; `0` clears all bands so zero pings are planned.
- `H` opens the local consumption history.
- `Q` or `Esc` exits.

The hello can affect the current usage window, but it cannot restart a window already in progress. Reset countdowns use timestamps returned by Codex.

The app records quota snapshots, Codex's daily token-activity totals, and per-response token counts from local session logs. It keeps 90 days of history in SQLite under `~/Library/Application Support/Codex Timer/history.sqlite3` on macOS, or `${XDG_DATA_HOME:-~/.local/share}/codex-timer/history.sqlite3` on Ubuntu. The local session importer stores token metadata only; it does not save prompts or responses. Those hourly counts cover Codex sessions on this device, while the account endpoint reports daily totals. Set `CODEX_TIMER_DATA_DIR` to move the data directory.
The reset anchor, band interval, and ping bands are stored locally in that database. Editing the anchor changes only the local plan; it cannot move before Codex's next reported reset. Bands must remain at least one minute after the previous row. Adding a band schedules a real ping; deleting it or clearing all bands removes that ping.

## Terminal commands

```sh
bin/codex-timer status
bin/codex-timer ping
bin/codex-timer ping --watch
bin/codex-timer watch
bin/codex-timer history --days 14
bin/codex-timer history --hourly
bin/codex-timer export --days 30 --output ~/Desktop/codex-usage.png
```

Run `bin/codex-schedule` to open an email-style composer. Tab and Shift+Tab move through send time, model, reasoning level, directory, and prompt body. On header fields, Up/Down also move focus; Enter opens field editing or a selection menu. Use the arrow keys and Enter to choose a model or reasoning level. On the send-time field, Left/Right adjust the time by five minutes, accelerating while held; Enter lets you type a date as `YYYY-MM-DD HH:MM`. In the prompt body, the arrows move the cursor and Ctrl+J inserts a newline; Tab and Shift+Tab move focus out of the body. Escape cancels a field edit or menu, and Ctrl+C exits the composer. Ctrl+S schedules the prompt. The initial send time is ten minutes from now.

```sh
bin/install-codex-scheduler
bin/codex-timer schedule list
```

The installer configures and starts a per-user service: a systemd user unit on Ubuntu or a launch agent on macOS. The service runs due prompts in saved Codex chats using the chosen directory, model, and reasoning level. `schedule list` shows each job's state, and `schedule cancel ID` cancels a pending job. Times use the computer's local timezone. Stopping the service leaves pending jobs in the local SQLite database; when restarted, it requeues jobs interrupted during execution.

On Ubuntu, manage the service with `systemctl --user start codex-scheduler` (or `stop`, `restart`, and `status`); use `enable` or `disable` to control startup at login (`disable --now` also stops it). On macOS, the launch agent starts at login; inspect it with `launchctl print gui/$(id -u)/com.codex-timer.scheduler`, stop it with `launchctl bootout gui/$(id -u)/com.codex-timer.scheduler`, and remove `~/Library/LaunchAgents/com.codex-timer.scheduler.plist` to disable it at login.

`status` reads the current server-reported reset times without sending a model request, and saves a history snapshot. `watch` continuously shows the countdown, saves snapshots, and notifies when Codex reports a reset.
`history` renders daily account token activity or falls back to recent 5-hour quota snapshots until token activity is available. `history --hourly` shows recent per-hour token totals reconstructed from this device's local session logs; press `T` in the TUI history screen to switch between daily and hourly views.
`export` writes daily account token activity, local hourly token counts, and 5-hour/weekly quota curves to a 2400 × 1960 PNG using a built-in renderer; it needs no optional packages. Hourly token totals are separate from quota percentages. The TUI's `E` key exports the same chart to the app's exports folder.

## Use from any shell directory

Add the project's `bin` directory to your `PATH` in `~/.bashrc` (Ubuntu) or `~/.zshrc` (macOS), replacing the path with the location of this checkout. Then open a new terminal or source the file:

```sh
export PATH="$HOME/path/to/codex-timer/bin:$PATH"
```

With it installed, `codex-timer` opens the full-screen app.

## Run the tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Implementation notes

The app uses the local `codex app-server` JSON-RPC interface. It reads `account/rateLimits/read` for reset timestamps. The App Server integration is documented as experimental, so a future Codex CLI update may require a protocol adjustment.

- [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
- [Codex CLI documentation](https://developers.openai.com/codex/cli/)
