# Chrome Skill Manual Checklist

Phase 16 validates the dedicated `ChromeSkill` plugin and `SkillsManager` routing layer.

## Preconditions

- Windows 10/11
- Google Chrome installed
- `chrome_skill_enabled = true`
- `preferred_browser = "chrome"`
- `auto_launch_chrome_if_needed = true` if you want automatic startup
- Optional for stronger page reading: `pywinauto` installed

## Skill Loading

- [ ] `SkillsManager.load_builtin_skills()` registers `ChromeSkill`
- [ ] `SkillsManager.list_skills()` reports Chrome capabilities and health
- [ ] Processor routes Chrome commands through the skill instead of inline browser logic

## Search

- [ ] `search IPL score`
- [ ] `google python tutorial`
- [ ] `find weather mumbai`
- [ ] Confirm assistant response says it is searching in Chrome
- [ ] Confirm Chrome is focused or launched first

## Tabs And Navigation

- [ ] `new tab`
- [ ] `open tab`
- [ ] `close tab`
- [ ] `go back`
- [ ] `go forward`
- [ ] `refresh page`
- [ ] `scroll down`
- [ ] `scroll up`

## Reading

- [ ] `read page title`
- [ ] `read results`
- [ ] `what is on page`
- [ ] `read first result`
- [ ] Confirm returned text came from the visible page title or accessible UI text only
- [ ] Confirm failures are honest when visible text cannot be detected

## Robustness

- [ ] Close Chrome and run a Chrome command
- [ ] Confirm auto-launch behavior matches settings
- [ ] Disable `chrome_skill_enabled` and confirm processor falls back cleanly
- [ ] Run repeated commands without stale tab or page-title state
- [ ] Confirm logs show skill selection, action, and read-results source
