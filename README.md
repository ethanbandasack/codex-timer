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
- `H` opens the local consumption history.
- `Q` or `Esc` exits.

The hello can affect the current usage window, but it cannot restart a window already in progress. Reset countdowns use timestamps returned by Codex.

The app records quota usage snapshots and Codex's daily token-activity totals locally. It keeps 90 days of history in SQLite under `~/Library/Application Support/Codex Timer/history.sqlite3` on macOS. Set `CODEX_TIMER_DATA_DIR` to move the data directory.

## Terminal commands

```sh
bin/codex-timer status
bin/codex-timer ping
bin/codex-timer ping --watch
bin/codex-timer watch
bin/codex-timer history --days 14
bin/codex-timer history --days 14
```

`status` reads the current server-reported reset times without sending a model request, and saves a history snapshot. `watch` continuously shows the countdown, saves snapshots, and notifies when Codex reports a reset.
`history` renders daily token activity as a terminal histogram, or falls back to recent 5-hour quota snapshots until token activity is available.

## Use from any zsh directory

Add this function to `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`:

```zsh
codex-timer() {
  "/Users/ethan/Documents/Autres/Playground/codex_timer/bin/codex-timer" "$@"
}
```

With it installed, `codex-timer` opens the full-screen app.

## Implementation notes

The app uses the local `codex app-server` JSON-RPC interface. It reads `account/rateLimits/read` for reset timestamps. The App Server integration is documented as experimental, so a future Codex CLI update may require a protocol adjustment.

- [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
- [Codex CLI documentation](https://developers.openai.com/codex/cli/)
