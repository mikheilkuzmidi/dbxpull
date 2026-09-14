# Dropbox Backup

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A high-performance, parallel backup tool for Dropbox with smart rate limiting, beautiful progress display, and automatic dependency folder filtering.

## Features

- **Parallel Downloads**: Configurable concurrent downloads (default: 6 threads)
- **Smart Rate Limiting**: Adaptive rate limiter that adjusts based on API responses
- **Exponential Backoff**: Automatic retry with jitter for failed requests
- **Resume Capability**: Skips already downloaded files automatically
- **Dependency Filtering**: Automatically skips `node_modules`, `venv`, `.git`, and 40+ other build folders
- **Beautiful Progress Display**: Real-time progress with speed, ETA, and per-file tracking
- **Folder Picker**: GUI dialog to select destination if not configured

## Installation

```bash
git clone https://github.com/mikheilkuzmidi/dropbox-backup.git
cd dropbox-backup
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

The recommended way to authenticate uses OAuth refresh tokens, which **never expire** and automatically refresh during long backups.

```bash
# Run the interactive authentication setup
dropbox-backup auth
```

This will:
1. Ask for your **App Key** and **App Secret** (found in your app's Settings tab)
2. Open a browser for you to authorize the app
3. Save the credentials to your `.env` file

That's it! Your backups will now run without token expiration issues.

### Alternative: Manual Configuration

If you prefer to configure manually, copy the example config:

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```ini
# RECOMMENDED: OAuth with auto-refresh (run 'dropbox-backup auth' to get these)
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

> **Note:** Legacy access tokens (from the "Generate" button) expire after 4 hours and are **not recommended** for long backups. Use `dropbox-backup auth` to set up auto-refreshing tokens instead.

> **Tip:** The app automatically loads `.env`, no need to run `source .env`.
>
> If you leave `DROPBOX_BACKUP_DEST` empty, the app will open a folder picker dialog.

## Usage

```bash
python3 -m dropbox_backup
```

Or after installation:

```bash
dropbox-backup
```

## Example Output

```
╔══════════════════════════════════════════════════════════════════════╗
║                          DROPBOX BACKUP                              ║
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

By default, these folders are skipped:

`node_modules`, `.npm`, `.yarn`, `venv`, `.venv`, `__pycache__`, `.git`, `build`, `dist`, `.next`, `.nuxt`, `.cache`, `.idea`, `.vscode`, `Pods`, `DerivedData`, and more.

## Troubleshooting

**"Authentication failed"**  
If using legacy access tokens, they expire after 4 hours. Run `dropbox-backup auth` to set up auto-refreshing OAuth tokens instead.

**"Token expired during backup"**  
This happens with legacy access tokens on long backups. The solution is to use OAuth refresh tokens:
```bash
dropbox-backup auth
```

**"Rate limited" messages**  
This is normal. The tool handles rate limits automatically with exponential backoff.

**Folder picker doesn't open**  
Make sure `tkinter` is installed. Alternatively, set `DROPBOX_BACKUP_DEST` in your `.env` file.

## License

MIT License, see [LICENSE](LICENSE) for details.
