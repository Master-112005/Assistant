# YouTube Skill Manual Checklist

Phase 17 validates the dedicated `YouTubeSkill` plugin and media-control routing.

## Preconditions

- Windows 10/11
- A supported desktop browser installed
- `youtube_skill_enabled = true`
- `auto_open_youtube_if_needed = true` if automatic startup/navigation is desired
- Optional for stronger result/title detection: `pywinauto` installed

## Skill Loading

- [ ] `SkillsManager.load_builtin_skills()` registers `YouTubeSkill`
- [ ] `SkillsManager.list_skills()` reports YouTube capabilities and health
- [ ] Processor routes YouTube/media commands through the skill instead of inline processor logic

## Search And Playback

- [ ] `search dulaander song on youtube`
- [ ] `play dulaander song` while YouTube is active
- [ ] `play first result`
- [ ] `play second result`
- [ ] Confirm YouTube search uses the in-page search box when available, with URL fallback only when needed
- [ ] Confirm the first visible result actually opens

## Playback Controls

- [ ] `pause`
- [ ] `resume`
- [ ] `next video`
- [ ] `previous video`
- [ ] Confirm the page is focused before shortcuts fire
- [ ] Confirm failures are honest when title/state verification is unavailable

## Volume

- [ ] `volume up`
- [ ] `volume down`
- [ ] `mute`
- [ ] Confirm volume changes are YouTube player controls, not system volume

## Reading

- [ ] `read current title`
- [ ] `what is playing`
- [ ] Confirm returned text came from the live page/window title or accessible UI only

## Robustness

- [ ] Close all browsers and run `search dulaander song on youtube`
- [ ] Disable `auto_open_youtube_if_needed` and confirm the failure is honest
- [ ] Repeat `pause`, `resume`, and `next video` commands several times
- [ ] Confirm logs show skill selection, search query, result-play action, and media-control actions
