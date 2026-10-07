# Turn on "Continue with Google"

Email + password sign-in works without any setup. The Google button needs a free
**OAuth client** from Google Cloud (about 10 minutes, no credit card).
You can use the same Google Cloud project you made for Google Calendar.

## 1. Consent screen (once)

1. Open https://console.cloud.google.com/ and pick your project (top bar).
2. Go to **APIs & Services → OAuth consent screen** (newer console: **Google Auth Platform → Branding**).
3. User type **External** → fill in:
   - App name: `Smart Study Planner`
   - User support email: your Gmail
   - Developer contact email: your Gmail
4. Scopes: add only `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`.
   These are basic scopes, so Google does not require app verification.
5. **Publishing status → Publish app** (or add classmates under *Test users*).
   While in *Testing*, only test users can sign in.

## 2. Create the web client

1. **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
2. Application type: **Web application**. Name: `Smart Study Planner web`.
3. **Authorized redirect URIs**, add all three:
   ```
   http://127.0.0.1:5000/auth/google/callback
   http://localhost:5000/auth/google/callback
   https://smart-study-planner-3wz3.onrender.com/auth/google/callback
   ```
4. Click **Create**, then **Download JSON**.

> This is a different client from your Calendar `credentials.json` (that one is a
> "Desktop app"). Keep both. Don't overwrite `credentials.json`.

## 3. Use it on your laptop

Rename the downloaded file to **`google_login.json`** and put it in the `StudyPlanner`
folder, next to `app.py`. Restart the app:

```powershell
.\venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. The Google button is now active.
(`google_login.json` is in `.gitignore`, so it never goes to GitHub.)

## 4. Use it on Render

Render → your service → **Environment** → add:

| Key | Value |
| --- | --- |
| `GOOGLE_CLIENT_ID` | the `client_id` from the JSON (ends in `.apps.googleusercontent.com`) |
| `GOOGLE_CLIENT_SECRET` | the `client_secret` from the JSON |

Save. Render restarts the app with Google sign-in on.

## Good to know

- The **first account** you create on your laptop keeps all your existing tasks,
  timetable and plans. Every other account starts empty and private.
- Google Calendar sync stays exactly as before and only for you (the first account,
  or set `STUDYPLANNER_OWNER_EMAIL=you@gmail.com`).
- If someone signs in with Google using the same email as an existing
  email/password account, the two are linked.
- Error `redirect_uri_mismatch`: the address in your browser must match one of the
  redirect URIs exactly. Use `127.0.0.1:5000` or `localhost:5000` as registered.
- On Render's free plan the database resets on restart/redeploy, so online accounts
  are temporary. For permanent accounts, add a Render persistent disk and set
  `STUDYPLANNER_DB=/var/data/database.db`.
