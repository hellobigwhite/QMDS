# WP Menu Opener

Chrome Manifest V3 extension for logging in to a WordPress backend and opening `wp-admin/nav-menus.php`.

## Usage

1. Open Chrome `chrome://extensions/`.
2. Enable `Developer mode`.
3. Click `Load unpacked` and select this folder.
4. Click the extension icon, enter domains such as `example.com`, one per line, then click the batch login button.

The extension posts directly to `https://www.[domain]/bbwllogin/` and derives the username as `Ad[domain-without-.com]min`. For `example.com`, the username is `Adexamplemin`. The target opened page is `https://www.example.com/wp-admin/nav-menus.php`.

You can paste multiple domains at once:

```text
example.com
demo.com
https://www.test.com/wp-admin
```

Duplicate entries are processed once. The popup also accepts line breaks, spaces, English commas, and Chinese commas as separators.

## Notes

- The login request runs in the background and is not opened as a tab.
- The password is currently stored in `background.js` as requested.
- The extension assumes WordPress is available under `https://www.[domain]`.
