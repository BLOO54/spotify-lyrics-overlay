# Getting your Windows installer from GitHub (no coding)

GitHub lends out free Windows computers that build the app for you. You upload the files once,
and a few minutes later you download the finished installer.

### 1. Make a free GitHub account
Go to <https://github.com/signup> and sign up.

### 2. Create a repository (a project folder on GitHub)
1. Click the **+** at the top-right → **New repository**.
2. Name it `spotify-lyrics-overlay`.
3. Choose **Public** (unlimited free build minutes) or **Private** (free minutes are limited, but plenty for this).
4. Click **Create repository**.

### 3. Upload the files
1. Unzip `SpotifyLyricsOverlay-source.zip` on your PC (right-click → **Extract All**).
2. On your new repository page, click the link **"uploading an existing file"**.
3. Open the extracted `SpotifyLyricsOverlay` folder, select **everything inside it** (Ctrl+A),
   and drag it into the browser window.
   > ⚠️ The hidden folder `.github` must be included. If you don't see it in File Explorer, click
   > **View → Show → Hidden items** first. Without it, nothing gets built.
4. Click **Commit changes**.

**If the Actions tab stays empty** (the `.github` folder didn't upload):
1. On the repository page click **Add file → Create new file**.
2. In the name box type exactly: `.github/workflows/build-windows.yml`
3. Open that same file from the zip in Notepad, copy everything, paste it into the browser, and click **Commit changes**.

### 4. Wait for the build (about 5–8 minutes)
1. Click the **Actions** tab. You'll see **"Build Windows app"** running (yellow dot).
2. When it turns into a **green ✔**, click it.
3. Scroll down to **Artifacts** and download **SpotifyLyricsOverlay-Windows-1.0.0**.
4. Unzip it. Inside is **`SpotifyLyricsOverlay-Setup-1.0.0.exe`**. That's your installer! 🎉

If you see a **red ✘** instead, click it, then click the failed step to see the error.
Copy that text and send it to Claude to fix.

**The build also tests the app on Windows:** it runs a self-check of the finished .exe and
saves a picture of the overlay. Download **test-results** from the same page to see them.
