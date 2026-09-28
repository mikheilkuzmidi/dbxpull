# dbxpull

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Pull a whole Dropbox account down to a local or external drive, in parallel, with adaptive rate limiting, a live progress display, automatic dependency folder filtering, and Dropbox content-hash verification.

Not a backup tool: there is no version history and no schedule. It is a resumable bulk download, which is what the name says.

![The tail of a real pull, and the summary it ends on](docs/dbxpull.gif)

This recording shows an earlier version pulling a real account: 164 files and 751 MB in 44 seconds at 16.9 MB/s, six downloads at a time, with two rate limit hits handled by backing off rather than failing. Version 1.1 adds a local read-back verification pass, so timings will depend on drive speed as well as the connection.

## Features

- **Parallel Downloads**: Configurable concurrent downloads (default: 6 threads)
- **Smart Rate Limiting**: Adaptive rate limiter that adjusts based on API responses
- **Exponential Backoff**: Automatic retry with jitter for failed requests
- **Corruption Verification**: Reads saved files back and checks their size and Dropbox content hash before marking a download complete
- **Verified Resume**: Skips an existing file only when its size and content hash match; downloads a fresh copy when either differs
- **Dependency Filtering**: Automatically skips `node_modules`, `venv`, `.git` and 50 other build and dependency folders
- **Beautiful Progress Display**: Real-time progress with speed, ETA, and per-file tracking
- **Folder Picker**: GUI dialog to select destination if not configured

## Installation

```bash
git clone https://github.com/mikheilkuzmidi/dbxpull.git
cd dbxpull
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

## Setup

### 1. Create a Dropbox App

1. Go to the [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. Click **Create app**
3. Choose **Scoped access** → **Full Dropbox**
4. Name your app and click **Create app**

### 2. Configure Permissions

In the **Permissions** tab, enable:
- `files.metadata.read`
- `files.content.read`
- `account_info.read`

Click **Submit**.

### 3. Set Up Authentication (Recommended: OAuth with Auto-Refresh)

The recommended way to authenticate uses OAuth refresh tokens, which automatically renew access tokens during long downloads. Refresh tokens can still be revoked.

```bash
# Run the interactive authentication setup
dbxpull auth
```

This will:
1. Ask for your **App Key** and **App Secret** (found in your app's Settings tab)
2. Give you a browser URL to authorize the app
3. Save the credentials to your `.env` file

The downloader can now renew its access token during long runs.

### Alternative: Manual Configuration

If you prefer to configure manually, copy the example config:

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```ini
# RECOMMENDED: OAuth with auto-refresh (run 'dbxpull auth' to get these)
DROPBOX_APP_KEY="your_app_key"
DROPBOX_APP_SECRET="your_app_secret"  
DROPBOX_REFRESH_TOKEN="your_refresh_token"

# Backup destination
DROPBOX_BACKUP_DEST="/path/to/backup/folder"

# OPTIONAL
DROPBOX_ROOT_PATH=""                    # Folder to backup (empty = all)
DROPBOX_CONCURRENT_DOWNLOADS="6"        # Parallel downloads
DROPBOX_MAX_GB_PER_RUN="0"              # Limit per run (0 = unlimited)
```

> **Note:** Legacy access tokens (from the "Generate" button) expire after 4 hours and are **not recommended** for long backups. Use `dbxpull auth` to set up auto-refreshing tokens instead.

> **Tip:** The app automatically loads `.env`, no need to run `source .env`.
>
> If you leave `DROPBOX_BACKUP_DEST` empty, the app will open a folder picker dialog.

## Usage

```bash
python3 -m dbxpull
```

Or after installation:

```bash
dbxpull
```

## Verification and resume

Verification is always enabled. No extra flag or Dropbox permission is needed.

1. The scan records each selected file's size, revision and `content_hash`.
2. Downloads request that exact revision and stream into a unique `.part` file on the destination drive.
3. After flushing and closing the file, dbxpull reads the saved bytes and checks both size and Dropbox's content hash.
4. Only a verified file replaces the final destination. A failed transfer or integrity check is retried up to `DROPBOX_MAX_RETRIES` attempts (default: 5). If all attempts fail, the existing destination is preserved and the failure is reported in the summary and `dbxpull.log`.
5. On the next run, existing files are read and checked again before they are skipped. This detects same-size corruption and same-size changes in Dropbox, including files downloaded by older versions.

The hash follows [Dropbox's content-hash algorithm](https://www.dropbox.com/developers/reference/content-hash): SHA-256 of each 4 MiB block, followed by SHA-256 of the concatenated binary block digests. It is not the ordinary SHA-256 checksum of the whole file. Verification uses bounded memory and adds local disk reads, including on resume.

Missing or invalid hashes are failures, never a fallback to size-only checks. An incomplete Dropbox scan aborts the run instead of accepting a partial file list. The summary reports how many files were verified. A dry run reports planned downloads separately and does not claim they were downloaded or verified.

Ctrl+C stops the run. Completed files are verified again next time; an unfinished file restarts from the beginning. Partial files are cleaned up on handled failures and interruptions. A force-kill or power loss may leave a hidden `.dbxpull-*.part` file, which is never treated as a finished download. These can be removed when no dbxpull process is running.

Verification covers the selected Dropbox revisions and the local bytes read during this run. Run again to check the current contents later. Filtering still applies, and this tool does not retain version history, preserve all filesystem metadata, or remove local files deleted in Dropbox.

If a run limit is set, whole files that do not fit within the remaining budget are deferred. Increase or remove the limit to download a file larger than that budget. The limit counts selected file sizes, not network bytes spent on retries.

| Exit code | Meaning |
| --- | --- |
| `0` | Successful run, successful dry run, or cancellation before downloading |
| `1` | Configuration, scan, download or verification failure |
| `2` | Files remain because of the configured run limit |
| `130` | Interrupted with Ctrl+C or a termination signal |

## Example Output

```
╔══════════════════════════════════════════════════════════════════════╗
║                               DBXPULL                                ║
╠══════════════════════════════════════════════════════════════════════╣
║ Parallel Downloads  •  Smart Rate Limiting  •  Exponential Backoff   ║
╚══════════════════════════════════════════════════════════════════════╝

─── Configuration ─────────────────────────────────────────────────────
  ✓ Configuration valid
  ✓ Destination: /Volumes/Backup/Dropbox
  ℹ Disk: 450.2 GB free / 1000.0 GB total

  ✓ Connected to Dropbox

─── Downloading ───────────────────────────────────────────────────────
  [████████████████░░░░░░░░░░░░░░] 52.3%  4.45 GB / 8.5 GB
  Speed:   12.5 MB/s  ETA:      5m 23s  Files: 6543/12543  Active: 6
```

## Skipped Folders

Fifty three folder names are skipped by default, the full list being `DEFAULT_SKIP_DIRS` in `src/dbxpull/config.py`:

`node_modules`, `.npm`, `.yarn`, `.pnpm-store`, `bower_components`, `venv`, `.venv`, `env`, `__pycache__`, `site-packages`, `.git`, `.hg`, `.svn`, `build`, `_build`, `dist`, `out`, `target`, `.next`, `.nuxt`, `.svelte-kit`, `.angular`, `.expo`, `.turbo`, `.parcel-cache`, `.webpack`, `.gradle`, `.maven`, `cmake-build-debug`, `cmake-build-release`, `Pods`, `DerivedData`, `.idea`, `.vscode`, `.vs`, `.eclipse`, `.settings`, `.cache`, `.mypy_cache`, `.pytest_cache`, `.tox`, `.nox`, `.eggs`, `.build`, `.env`, `logs`, `.logs`, `temp`, `.temp`, `tmp`, `.tmp`, `vendor`, `.bower_components`.

Directory matching is case-insensitive. Only parent directory names are matched, so an ordinary file such as `/project/.env` or `/project/build` is included. Turn off dependency filtering at the prompt to include these directories too.

## Troubleshooting

**"Authentication failed"**  
If using legacy access tokens, they expire after 4 hours. Run `dbxpull auth` to set up auto-refreshing OAuth tokens instead.

**"Token expired during backup"**  
This happens with legacy access tokens on long backups. The solution is to use OAuth refresh tokens:
```bash
dbxpull auth
```

**"Rate limited" messages**  
This is normal. The tool handles rate limits automatically with exponential backoff.

**Folder picker doesn't open**  
Make sure `tkinter` is installed. Alternatively, set `DROPBOX_BACKUP_DEST` in your `.env` file.

## Development and tests

```bash
python -m pip install -e ".[dev]" build
python -m pytest --cov=dbxpull --cov-report=term-missing
python -m ruff check .
python -m mypy src/dbxpull
python -m build
```

The tests compare hashes with fixed vectors generated by Dropbox's reference implementation at empty-file and 4 MiB block boundaries. Download tests cover same-size corruption, truncated and oversized transfers, changed metadata, read-back corruption, disk errors, retries, interruption, cleanup, destination preservation, real `.part` filenames, dry runs and concurrent run limits.

The end-to-end suite launches the CLI in a subprocess and uses the real Dropbox SDK against a local HTTP test server. It exercises OAuth refresh, paginated scanning, filters, downloads, verified resume, corruption repair, rate limits and failure exit codes. All tests run locally with test data and dummy credentials. No `.env` file, Dropbox app, personal credentials or repository secrets are required, and the tests do not contact a real Dropbox account.

## License

MIT License, see [LICENSE](LICENSE) for details.
