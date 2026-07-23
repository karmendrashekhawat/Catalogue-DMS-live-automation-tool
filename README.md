# DealerCenter Media Uploader

Automates bulk VIN photo replacement across multiple DealerCenter accounts.
Built for Spyne. Runs locally, driving a real Chrome window with Playwright.

For each account it logs in (pulling the MFA code from Gmail automatically), then for
every VIN folder it: filters the VIN → opens the record → **Remove All** old photos →
uploads that VIN folder's images → **Save and Close** → **Reset** → next VIN.

---

## Data folder layout

Select the top folder (your "Live" folder). Account subfolder names must match the
`DC_ACCOUNT_n_NAME` values in `.env.local` (spacing/case are matched loosely).

```
Live/
├── International Auto/
│   ├── 1FMEU73EX8UB24500/
│   │   ├── 1_1_Exterior.jpg
│   │   └── ...
│   └── JF2SHADCXCH416527/
├── Mega Motors/
│   └── <VIN>/ ...
├── Capitol/
├── Capitol of Smithfield/
└── Capitol Auto of Zebulon/
```

Only `.jpg`, `.jpeg`, `.png` are uploaded (DealerCenter's supported formats).

---

## One-time setup

1. Install **Python 3.8+** (check "Add Python to PATH" on Windows).
2. Copy `.env.local.example` → `.env.local` and fill in:
   - Each account's DealerCenter username/password.
   - `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` for the inbox that receives the codes
     from `do-not-reply@dealercenter.net`.
3. **Gmail app password:** turn on 2-Step Verification, then
   Google Account → Security → App passwords → generate one for "Mail" and paste it
   as `GMAIL_APP_PASSWORD` (the tool strips spaces automatically).

---

## Run

- **Windows:** double-click `start_windows.bat`
- **Mac/Linux:** `chmod +x start_mac.sh && ./start_mac.sh`

The control panel opens at **http://localhost:7433**. Paste your Live folder path and
click **Start upload**. If the Gmail fetch ever fails, the panel shows a box to type the
code in manually and continue.

---

## How MFA is handled

After clicking **Continue**, DealerCenter emails a 6-digit code. The tool polls the
configured Gmail inbox over IMAP, reads only messages from `do-not-reply@dealercenter.net`
that arrived after the login click, extracts the code (`Your code is: NNNNNN`), and enters
it. A fresh browser session is used per account, so each account gets a clean login.

---

## Files

```
server.py            HTTP control panel + Playwright automation
otp_reader.py        Gmail IMAP → DealerCenter MFA code
config.json          non-secret settings (url, port, extensions, timeouts)
.env.local           YOUR secrets (git-ignored — never commit)
.env.local.example   template
ui/index.html        control panel
start_windows.bat / start_mac.sh   launchers
```

---

## Tuning selectors

DealerCenter's HTML class names aren't public, so `server.py` uses text-based selectors
(`Continue`, `Active Inventory`, `Stock# or VIN#`, `Run`, `Media`, `Remove All`, `Yes`,
`Save and Close`, `Reset`) with fallbacks. If a step is missed on the live site, the
activity log names the exact step — adjust that step's selector list in `server.py`.

---

## Security

- `.env.local` holds real credentials and is git-ignored. Never commit or share it.
- Only automate accounts you own or are authorized to manage.
- Automating logins/MFA may be restricted by DealerCenter's Terms of Service — confirm
  you have permission before running.
