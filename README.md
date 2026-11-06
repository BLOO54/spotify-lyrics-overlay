# 🎤 Spotify Lyrics Overlay

A transparent, always-on-top lyrics window for **Windows 10 and 11**. It shows the lyrics
of whatever you're playing in Spotify, karaoke-style, on top of your games.

- Follows Spotify automatically (song, artist, play/pause, skipping, seeking)
- Word-by-word highlighting with a smooth purple→blue wipe
- Works over **borderless / windowed fullscreen** games
- **Click-through** mode so it never gets in the way of your mouse
- Custom font, size, colours, opacity and position, plus **8 theme presets**
- **6 highlight styles**: wipe, pop, glow, grow, bounce or underline
- Optional **album art**, **song timer** (time left or elapsed/total) and progress bar
- **Lyrics-only mode** with no background box
- **Translation** of foreign-language songs, and **romaji** for Japanese songs
- Lives in the system tray, with keyboard shortcuts that work in-game

**No Spotify login, no password, no API keys.** The app only *reads* the "now playing" info that
Spotify already shares with Windows (the same info shown in the Windows volume popup).
It never touches Spotify or your games.

---

## 📥 Installing (no programming needed)

1. Download **`SpotifyLyricsOverlay-Setup-1.8.0.exe`** (see "Where do I get the installer?" below).
2. Double-click it.
3. **Windows may show a blue "Windows protected your PC" box.** This is normal for free apps
   that aren't paid-signed. Click **More info** → **Run anyway**.
4. Click **Next** through the installer. You can tick "Create a desktop shortcut" and
   "Start automatically when I sign in".
   *It installs just for your user account, so it never asks for administrator permission.*
5. Leave "Start Spotify Lyrics Overlay now" ticked and click **Finish**.

**Prefer no installer?** Download `SpotifyLyricsOverlay-Portable-1.8.0.zip` instead, right-click → **Extract All**,
open the folder and double-click **`SpotifyLyricsOverlay.exe`**.

### Where do I get the installer?
The installer is built automatically by GitHub on a real Windows computer:
* On the project's GitHub page, click **Releases** (right side) and download the `.exe`, **or**
* Click the **Actions** tab → the newest run with a green ✔ → scroll to **Artifacts** →
  download **SpotifyLyricsOverlay-Windows-…** (it's a zip that contains the installer).

Don't have the project on GitHub yet? See **[BUILD_ON_GITHUB.md](BUILD_ON_GITHUB.md)**.
It's about 5 minutes of clicking, with no coding.

---

## ▶️ Using it

1. Open **Spotify** (the desktop app) and play a song.
2. Start **Spotify Lyrics Overlay** (Start menu or desktop shortcut).
3. Lyrics appear near the bottom of your screen. 🎶

The app runs in the **system tray** (the small icons at the bottom-right of the taskbar; click the
**^** arrow if you don't see it). **Right-click the tray icon** for the menu:

| Menu item | What it does |
|---|---|
| Show overlay | Hide or show the lyrics |
| Click-through | When ON, your mouse goes straight through the overlay to the game |
| Settings… | Font, size, colours, opacity, position and more |
| Lyrics earlier / later | Fix lyrics that feel out of sync |
| Reload lyrics | Download the lyrics for the current song again |
| Quit | Close the app |

### ⌨️ Keyboard shortcuts (work while gaming)

| Keys | Action |
|---|---|
| **Ctrl + Alt + L** | Show / hide the overlay |
| **Ctrl + Alt + K** | Click-through on / off |
| **Ctrl + Alt + O** | Open settings |
| **Ctrl + Alt + ←** | This song's lyrics earlier (−250 ms), remembered for this song |
| **Ctrl + Alt + →** | This song's lyrics later (+250 ms), remembered for this song |
| **Ctrl + Alt + V** | Try another lyrics version for this song (remembered) |

**Change the shortcuts:** Settings → **Shortcuts**. Click a button, then press the keys you want
(for example Ctrl+Shift+L, or a single key like F8). Backspace turns a shortcut off, Esc cancels.
"Use shortcuts without Alt" switches them all to Ctrl+Shift + the same key. If another program already
uses a combination, the Shortcuts tab tells you so you can pick a different one.

### 🖱️ Moving the overlay
1. Turn **click-through OFF** (Ctrl+Alt+K).
2. Drag the overlay with the left mouse button and drop it where you want it.
3. Turn click-through back **ON** before gaming.

**How many lines?** Settings → Behaviour → **Lines shown**: 1 (current line only), 2 (current + next)
or 3 (previous, current and next, the default).

Or pick a preset position in **Settings → Position & size** (top, bottom, corners, centre, and which monitor).
Right-click the overlay or double-click it to open the menu or settings.

### 🎮 Games
- ✅ **Borderless windowed / "windowed fullscreen"**: works. Set this in your game's video settings.
- ❌ **Exclusive fullscreen**: no overlay app can draw over this without injecting into the game,
  which this app deliberately does **not** do (it could trigger anti-cheat). Switch the game to borderless.

### Word-by-word vs line-by-line
The header shows which sync mode is in use:
- **word sync**: the lyrics come with real per-word timing, so the highlight is exact.
- **line sync · word estimate**: only line timing exists. The app spreads the words across the line so you still
  get a karaoke feel. Turn off *"Estimate word timing"* in Settings for plain line-by-line mode,
  where the whole line lights up with a progress bar underneath.

Lyrics come from **[LRCLIB](https://lrclib.net)**, a free, community-made lyrics database. The app prefers
word-timed lyrics, then line-timed lyrics, and only falls back to untimed lyrics if nothing else exists.
Most songs only have line timing there.

**Word timing guess** (Settings → Lyrics & translation) decides how the words are spread when only line timing
exists: **Smart** (default) gives each word time for the syllables actually sung (mora for Japanese, one per
Korean/Chinese character) and adds a short breath after commas; **Simple** shares the time by number of letters. If a song isn't in LRCLIB, the overlay says "No lyrics found".

### 🎨 Settings at a glance
Open Settings (**Ctrl+Alt+O**, or right-click the tray icon). There are four tabs:

| Tab | What's there |
|---|---|
| **Look** | Theme presets (Neon, Ocean, Sunset, Sakura, Arcade, Gold, Minimal, Light), font, colours, **highlight style**, glow, "keep sung words coloured", **background box on/off**, **solid or frosted-glass background**, **corner rounding**, opacity, **match colours to the album art**, smooth colour fades |
| **Layout** | Lines shown (1/2/3), song title, **album art** and where it goes, **song timer**, progress bar, screen position, monitor, size |
| **Lyrics & translation** | Sync adjustment, word-timing estimate, **translation**, **romaji** |
| **Behaviour** | Click-through, hide while paused, Spotify only |

Pick a theme first, then tweak anything you like (the theme then shows as "Custom").

**Long lines** (Settings → Layout → *Long lines*) - pick what happens when a line is longer than the overlay:
* **Make the overlay wider** (default): the box stretches to fit the line on one row, but never past the edge
  of your screen. If a line is still too long at full screen width, its text shrinks a little.
* **Shrink the text to fit**: the box stays exactly the size you set; long lines get smaller text.
* **Wrap onto more rows**: long lines continue on a second or third row.

In every mode the overlay stays fully on screen. With **Album art position → Auto**,
the cover sits small next to the song title when the overlay is narrow, and big on the left when it's wide.

### ✨ Frosted glass and album colours
* **Background style → Frosted glass** *(experimental)* blurs whatever is behind the overlay (your game), like the Windows Start
  menu. It needs *Transparency effects* turned on in Windows Settings → Personalization → Colors; if it's off,
  the normal background is used instead. On PCs without a graphics card (or over Remote Desktop) Windows
  shows it as a plain dark panel - switch back to **Solid** if that happens.
* **Match the album art** picks the highlight colours and/or a dark background tint from the current album
  cover, and **fades smoothly** to the new colours when the song changes.
* **Corner rounding** sets how round the corners are (0 = square).

### 🌍 Translation and romaji
Turn on **"Translate songs that aren't in my language"** in *Lyrics & translation* and pick your language.
The app detects the song's language and only translates songs that aren't already in yours.
**Show translation** chooses the layout:
* **Translation big** (default): the translation is the big karaoke line, and the original lyrics sit small
  underneath. The highlight moves through the translated words in the same time the original line is sung.
* **Original big**: the original lyrics stay big, with the translation in small italics underneath.

While a song is being translated you'll see "translating…" for a few seconds.

* **MyMemory** (default): free, no account. About 5,000 characters a day (≈5–10 songs).
  Type **any email address** in the box to raise that to 50,000 a day.
* **DeepL**: better quality, 500,000 characters a month free. Make a free account at
  <https://www.deepl.com/pro-api>, copy your **API key**, and paste it into Settings.
  The key is saved only on your PC.
* Each song is translated **once** and remembered, so replaying it costs nothing.
* **Romaji** (Japanese pronunciation in English letters) is worked out on your PC, with no internet and no limits.

### Try it without Spotify
Start menu → **Spotify Lyrics Overlay (Demo mode)** plays a fake song so you can tweak the look.
For a Japanese demo song (to try translation/romaji), run `SpotifyLyricsOverlay.exe --demo --demo-song jp`.

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| "Waiting for Spotify…" | Use the Spotify **desktop app** (not the web player) and press play. In Windows Settings → System → Notifications, make sure Spotify isn't blocked. |
| One song's lyrics are early or late | Press Ctrl+Alt+← / → while it plays; the fix is remembered for that song. |
| One song's lyrics drift further out of time as it plays | The lyrics were timed for a different version of the song. Press **Ctrl+Alt+V** to try another version. |
| Every song's lyrics are early or late | Settings → Lyrics & translation → Sync adjustment (all songs). |
| Overlay hidden behind the game | Set the game to **borderless windowed**. |
| Can't click or move the overlay | Click-through is on. Press **Ctrl+Alt+K**. |
| Can't find the app | Look in the system tray (^ arrow bottom-right). |
| Shortcut keys don't work | Another program already uses them, or a key on your keyboard is broken. Change them in Settings → Shortcuts. |
| "Can't reach the lyrics service" | Check your internet. The app retries automatically every 15 seconds. |
| Translation stopped appearing | You've probably used today's free MyMemory allowance. Add an email or a DeepL key in Settings. |
| Japanese shows as boxes | Install the Japanese language pack: Windows Settings → Time & language → Language → Add Japanese. |
| Anything else | Start menu → **Spotify Lyrics Overlay (Self test)** checks every part and tells you what's wrong. The log is at `%APPDATA%\SpotifyLyricsOverlay\app.log`. |

**Uninstall:** Windows Settings → Apps → Spotify Lyrics Overlay → Uninstall.
Your settings stay in `%APPDATA%\SpotifyLyricsOverlay`; delete that folder to remove them too.

---

## 🔒 Privacy and fair use
- No account, login, password or token is used or stored.
- Internet requests go to `lrclib.net` (song title, artist, album and length, identified with an app User-Agent as LRCLIB asks)
  and, **only if you turn translation on**, the lyric lines go to MyMemory or DeepL to be translated.
- Lyrics are cached on your PC so each song is only downloaded once.
- Nothing is injected into games or Spotify; the overlay is a normal (transparent) window.

---

## 👩‍💻 For developers

```
app/
  lrc.py              LRC + enhanced (word-level) LRC parsing, word-timing estimation
  clock.py            playback position tracking between Windows updates
  lyrics_provider.py  LRCLIB client (lookup → search fallback, disk cache, offline errors)
  media_session.py    Windows GlobalSystemMediaTransportControls reader (winrt)
  win32.py            click-through, topmost, global hotkeys (ctypes / user32)
  overlay.py          the transparent window and karaoke rendering
  settings_dialog.py  settings window
  controller.py       wires it together, tray icon
  demo.py             demo songs for --demo
  themes.py           theme presets and highlight styles
  translate.py        language detection, MyMemory / DeepL translation, romaji (pykakasi)
main.py               entry point (--demo, --self-test)
SpotifyLyricsOverlay.spec   PyInstaller recipe
installer/installer.iss     Inno Setup installer (per-user, no admin)
.github/workflows/build-windows.yml   Windows build + tests + installer
```

Run from source on Windows (Python 3.10–3.12):
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest -q
.venv\Scripts\python main.py --demo
```

Make a release: bump `APP_VERSION` in `app/__init__.py` and `version_info.txt`, then push a tag
like `v1.0.1`. GitHub Actions builds and publishes the installer to Releases.
