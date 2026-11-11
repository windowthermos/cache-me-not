# Cache Cleaner

A small, minimal macOS app that frees disk space safely. It clears caches, finds leftovers from apps you have already uninstalled, and always lets you review before anything is removed.

Built with Python and [pywebview](https://pywebview.flowrl.com/), with a clean interface that follows the current macOS design language, including dark mode.

## Why

Most Mac cleaners are closed-source, subscription-based and ask for broad access. Cache Cleaner is a single readable Python file with one rule: **it must never harm the system**.

It was written for a Mac that runs local AI tools (MLX, ComfyUI, Ollama and similar), which leave large caches and model folders behind.

## What it does

**One-click clean (switches you control)**
- Desktop-app and Electron caches
- Browser caches (Chrome, Edge, Brave, Arc, Firefox, Safari)
- Other app caches and old logs
- Developer caches (pip, npm, Yarn, uv, Homebrew), off by default

**Review first (you pick; everything goes to the Trash)**
- Leftovers from removed apps, with a lock icon where a password is needed
- Old installers in Downloads
- Empty Trash, with a confirmation

**What it never touches:** cookies, logins, Keychain, Apple system caches, or app data.

Items are moved to the Trash the way Finder does it, so **Put Back** works. Every action is written to `~/Library/Logs/CacheCleaner.log`.

## Run it

Requires macOS and Python 3.

```bash
pip install -r requirements.txt
python3 cache_cleaner.py
```

## Safety notes

- Review the code before running any cleaner on your own machine, including this one.
- Leftover detection compares what is installed with what is left behind, so it can occasionally list something you still want. Review the ticked items before you confirm. Everything is moved to the Trash first, and nothing is deleted permanently unless you choose Empty Trash.
- Use at your own risk, as stated in the licence.

## Contributing

Issues and pull requests are welcome. Ideas: more browser and app profiles, a packaged `.app` build, and a dry-run mode.

## Licence

MIT. See [LICENSE](LICENSE).
