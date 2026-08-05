# Windows installer compatibility

## Summary
The installer currently fails on Windows in two ways:

- the install log writer crashes with `UnicodeEncodeError` when the default console code page cannot encode the log text
- `scripts/install.py --link` fails when symlink creation is blocked by Windows privilege policy (for example `WinError 1314`), leaving the CLI unusable even though the rest of the install works

## What changed
- switched install log writing to UTF-8 so Windows can write log output without crashing
- added a Windows fallback that writes a `playwrong.cmd` launcher when symlink creation is not permitted
- added regression tests for both behaviors

## Verification
Verified locally on Windows with:

- `python -m unittest discover -s tests -p 'test_install_windows.py'`
- `python scripts/install.py --link`
