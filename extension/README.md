# gallery-dl-web browser extension

A tiny Manifest V3 extension (Chrome / Edge / Brave / other Chromium) that sends **your own
logged-in** Instagram and Facebook cookies to your gallery-dl-web server — one click, refreshable.
It replaces the need to copy-paste cookies by hand into the Settings page.

> It only ever reads cookies from **your own browser** and sends them to a server URL **you** type
> in. It never touches anyone else's session.

## Install (load unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Pin the "gallery-dl-web cookie sender" extension to the toolbar.

## Use

1. Click the extension icon.
2. In **gallery-dl-web server URL**, enter your server (the frontend), e.g.
   `http://192.168.1.13:3100`. It's saved locally in the browser.
3. Log into **instagram.com** (and/or **facebook.com**) in this browser.
4. Click **Send Instagram session** (or **Send Facebook cookies**). The status line shows
   `Instagram ✓` / `Facebook ✓`.
5. Download as usual in gallery-dl-web — no manual cookie paste needed.

**When it stops working** (IG/FB rotated or you logged out): just log back in and click the button
again. That's the whole point — one-click refresh.

## Appearance

The 🖥️ / ☀️ / 🌙 buttons in the popup header pick **System**, **Light** or **Dark**, matching the
web app. System is the default and follows your OS setting. The choice is remembered in this
browser and applied before the popup paints, so it never flashes the wrong theme.

It is **separate from the web app's setting** — a browser extension and a web page are different
origins and cannot share storage, so setting one does not change the other.

## What it does, exactly

- **Instagram:** reads the `sessionid` cookie for `instagram.com` and PUTs
  `{ig_sessionid: <value>}` to `PUT <server>/api/settings/cookies`.
- **Facebook:** reads all `facebook.com` cookies, converts them to Netscape `cookies.txt` text, and
  PUTs `{fb_cookies_text: <text>}` to the same endpoint.

The backend (`gallery-dl-web`) is unchanged — the extension feeds the exact endpoint the manual
Settings form uses.

## Permissions (why each)

- `cookies` + `host_permissions` for `*.instagram.com` / `*.facebook.com` — to read your own
  logged-in cookies for those two sites.
- `host_permissions` for `http://*/*` and `https://*/*` — so the extension can POST to whatever
  server URL you enter (your gallery-dl-web host, typically a LAN address). Broad by necessity.
- `storage` — to remember the server URL you typed.

## Security notes

- Cookies are sent over **HTTP on your LAN**. Only run this on a trusted network. If you expose
  gallery-dl-web beyond your LAN, put it behind HTTPS first.
- The extension is loaded unpacked by you; it is not on the Chrome Web Store (yet). Review the
  ~120 lines of `popup.js` — it does only what's described above. `theme.js` only reads and writes
  the theme preference; it touches no cookies and makes no network requests.
