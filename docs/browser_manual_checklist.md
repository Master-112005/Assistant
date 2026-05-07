# Browser Automation Manual Checklist

Phase 15 verification targets the desktop browser controller, not browser-specific web content.

## Preconditions

- Windows 10/11
- At least one supported browser installed: Chrome or Edge
- `pywin32`, `keyboard`, and `psutil` installed
- Optional for stronger element targeting: `pywinauto`, `pyautogui`, `uiautomation`
- Nova Assistant running with the updated settings defaults

## Basic Detection

- [ ] Open Google Chrome and confirm `detect_active_browser()` reports `chrome`
- [ ] Open Microsoft Edge and confirm `detect_active_browser()` reports `edge`
- [ ] Minimize the browser and confirm `focus_browser()` restores it
- [ ] With no browser open, confirm the assistant returns `No supported browser window found.` or launches the preferred browser honestly

## Search Flow

- [ ] `Search IPL score`
- [ ] `Search Python tutorial`
- [ ] `Search how to build a local voice assistant in Chrome`
- [ ] Confirm the browser is focused before text is submitted
- [ ] Confirm the address bar is selected with `Ctrl+L` or a fallback

## Navigation

- [ ] `Go back`
- [ ] `Go forward`
- [ ] `Refresh`
- [ ] `New tab`
- [ ] `Close tab`

## Scrolling

- [ ] `Scroll down`
- [ ] `Scroll up`
- [ ] Repeat scroll commands several times without losing browser focus

## Link / Result Interaction

- [ ] `Click first result`
- [ ] `Click second link`
- [ ] Confirm UIA path works when `pywinauto` is installed
- [ ] Confirm safe mode blocks uncertain heuristic clicks when verification is not possible
- [ ] Confirm disabling `safe_mode_clicks` allows the heuristic fallback

## Stability

- [ ] Re-run the same search command three times in a row
- [ ] Switch between Chrome and Edge and confirm the correct window is targeted
- [ ] Trigger commands on a multi-monitor setup and confirm window-relative clicks still land inside the browser window
- [ ] Confirm logs include browser detection, focus success, search submission, scroll attempts, and click failures
