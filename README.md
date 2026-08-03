# DMS Media Uploader

Automates bulk VIN photo replacement across multiple accounts on **two** DMS
platforms — DealerCenter and VinMotion. Built for Spyne. Runs locally, driving
a real Chrome window with Playwright.

For each account it logs in (pulling the verification code from Gmail
automatically), then for every VIN folder it:

- **DealerCenter:** filter VIN → open record → **Remove All** old photos →
  upload → **Save and Close** → back to Home → next VIN.
- **VinMotion:** search VIN → open record → Merchandising → **Select All** →
  **Delete** → **Upload** → **Save** → back to Inventory → next VIN.

---

## Data folder layout

Select the top folder (your "Live" folder). Account subfolder names must match
either a `DC_ACCOUNT_n_NAME` or a `VM_ACCOUNT_n_NAME` value in `.env.local`
(spacing/case are matched loosely) — you can mix accounts from both platforms
under the same root.

```
Live/
├── International Auto/            (DealerCenter)
│   ├── 1FMEU73EX8UB24500/
│   │   ├── 1_1_Exterior.jpg
│   │   └── ...
│   └── JF2SHADCXCH416527/
├── Mega Motors/                    (DealerCenter)
├── Capitol/                        (DealerCenter, shared login)
├── Capitol of Smithfield/
├── Capitol Auto of Zebulon/
├── Homan Auto Group/                (VinMotion)
└── Charlie clark - Harlingen/       (VinMotion, shared login)
```

Only `.jpg`, `.jpeg`, `.png` are uploaded.

---

## One-time setup

1. Install **Python 3.8+** (check "Add Python to PATH" on Windows).
2. Copy `.env.local.example` → `.env.local` and fill in:
   - Each account's username/password, under either the `DC_ACCOUNT_n_*` or
     `VM_ACCOUNT_n_*` prefix depending on which platform it's on.
   - `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` for the inbox that receives the
     verification codes from both `do-not-reply@dealercenter.net` (DealerCenter)
     and `msonlineserviceteam@microsoftonline.com` (VinMotion).
3. **Gmail app password:** turn on 2-Step Verification, then
   Google Account → Security → App passwords → generate one for "Mail" and
   paste it as `GMAIL_APP_PASSWORD` (the tool strips spaces automatically).

---

## Run

- **Windows:** double-click `start_windows.bat`
- **Mac/Linux:** `chmod +x start_mac.sh && ./start_mac.sh`

The control panel opens at **http://localhost:7433**. Paste your Live folder
path and click **Start upload**. If the Gmail fetch ever fails, the panel shows
a box to type the code in manually and continue.

---

## How verification codes are handled

After login, the DMS emails a 6-digit code. The tool polls the configured
Gmail inbox over IMAP, reads only messages from the matching sender that
arrived after the login click, extracts the code (`Your code is: NNNNNN` /
`code is: NNNNNN`), and enters it. A fresh browser session is used per login
group, so each group gets a clean login.

---

## Shared logins / rooftop switching

Some accounts share one login across several rooftops:

- **DealerCenter — Capitol group:** switches via the company name in the top
  bar → "Switch Dealership" list.
- **VinMotion — Charlie Clark group:** switches via the "VinMotion" text at
  top-left → rooftop list.

Set `ROOFTOP` (and, for DealerCenter, `COMPANY`) on each of those accounts in
`.env.local` and the tool switches in-app instead of logging out and back in.

---

## Files

```
server.py            HTTP control panel + Playwright automation (both DMS platforms)
otp_reader.py         Gmail IMAP -> verification code (shared by both platforms)
config.json           non-secret settings (URLs, port, extensions, timeouts)
.env.local             YOUR secrets (git-ignored — never commit)
.env.local.example    template
ui/index.html          control panel
start_windows.bat / start_mac.sh   launchers
```

---

## Tuning selectors

Neither platform's HTML class names are public, so `server.py` uses
text-based selectors with fallbacks. If a step is missed on the live site, the
activity log names the exact step — adjust that step's selector list in
`server.py` (DealerCenter functions are prefixed `dc_`, VinMotion `vm_`).

---

## Security

- `.env.local` holds real credentials and is git-ignored. Never commit or share it.
- Only automate accounts you own or are authorized to manage.
- Automating logins/verification codes may be restricted by each platform's
  Terms of Service — confirm you have permission before running.
