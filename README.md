# Codex Timer

Codex Timer is a small macOS terminal app for checking Codex reset windows, sending a tiny hello ping, and watching for resets. It uses the Codex CLI and the ChatGPT sign-in already on the machine. There are no Python runtime dependencies.

## Start the app

From this folder, run:

```sh
bin/codex-timer
```

Or double-click `Start Codex Timer.command`. The full-screen UI refreshes usage data every minute.

Keyboard controls:

- `P` sends a one-word hello using `gpt-5.6-luna` at low effort, then closes the in-memory chat.
- `R` refreshes usage immediately.
- `S` opens the programming menu, anchored to Codex's reported 5-hour reset. `+` adds a band after the previous row (default `05:01`, giving `08:01` then `13:02` for a `03:00` reset); `↑`/`↓` select rows; `←`/`→` shift the selected row and later rows by five minutes; `E` edits the selected local date and time; `I` changes the band interval and respaces existing bands; `X` deletes one band; `0` clears all bands so zero pings are planned.
- `H` opens the local consumption history.
- `Q` or `Esc` exits.

The hello can affect the current usage window, but it cannot restart a window already in progress. Reset countdowns use timestamps returned by Codex.

The app records quota snapshots, Codex's daily token-activity totals, and per-response token counts from local session logs. It keeps 90 days of history in SQLite under `~/Library/Application Support/Codex Timer/history.sqlite3` on macOS. The local session importer stores token metadata only; it does not save prompts or responses. Those hourly counts cover Codex sessions on this device, while the account endpoint reports daily totals. Set `CODEX_TIMER_DATA_DIR` to move the data directory.
The reset anchor, band interval, and bands are stored locally in that database. Editing the anchor changes only the local plan; it cannot move before Codex's next reported reset. Bands must remain at least one minute after the previous row and do not send pings automatically.

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

`status` reads the current server-reported reset times without sending a model request, and saves a history snapshot. `watch` continuously shows the countdown, saves snapshots, and notifies when Codex reports a reset.
`history` renders daily account token activity or falls back to recent 5-hour quota snapshots until token activity is available. `history --hourly` shows recent per-hour token totals reconstructed from this device's local session logs; press `T` in the TUI history screen to switch between daily and hourly views.
`export` writes daily account token activity, local hourly token counts, and 5-hour/weekly quota curves to a 2400 × 1960 PNG using a built-in renderer; it needs no optional packages. Hourly token totals are separate from quota percentages. The TUI's `E` key exports the same chart to the app's exports folder.

## Use from any zsh directory

Add this function to `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`:

```zsh
codex-timer() {
  "/Users/ethan/Documents/Autres/Playground/codex-timer/bin/codex-timer" "$@"
}
```

With it installed, `codex-timer` opens the full-screen app.

## Implementation notes

The app uses the local `codex app-server` JSON-RPC interface. It reads `account/rateLimits/read` for reset timestamps. The App Server integration is documented as experimental, so a future Codex CLI update may require a protocol adjustment.

- [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
- [Codex CLI documentation](https://developers.openai.com/codex/cli/)
