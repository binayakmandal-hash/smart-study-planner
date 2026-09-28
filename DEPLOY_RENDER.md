# Deploy Smart Study Planner on Render (public demo)

The online version runs in **demo mode**:
- Google Calendar sync is **off**. Your `credentials.json` / `token.json` are never uploaded.
- A banner tells visitors it is a shared demo.
- A **"Try the sample syllabus"** button builds a plan instantly from the bundled BCA Sem V syllabus.

Your local app is unchanged. Demo mode only switches on inside the Docker container.

---

## 1. Put the project on GitHub

`.gitignore` already keeps these out of Git: `credentials.json`, `token.json`, `database.db`, `venv/`, `uploads/` and `backups/`.

In PowerShell, inside `StudyPlanner`:

```powershell
git init
git add .
git status          # CHECK: credentials.json, token.json and database.db must NOT be listed
git commit -m "Smart Study Planner"
```

Create an **empty** repository on github.com (for example `smart-study-planner`, with no README), then:

```powershell
git branch -M main
git remote add origin https://github.com/<your-username>/smart-study-planner.git
git push -u origin main
```

> If `git status` ever shows `credentials.json` or `token.json`, stop and don't commit. Run `git rm --cached credentials.json token.json` first.

## 2. Create the service on Render

1. Sign in at https://render.com with your GitHub account.
2. **New + → Blueprint**, then pick your `smart-study-planner` repo.
   Render reads `render.yaml`, which sets up a free Docker web service with a random `FLASK_SECRET_KEY`.
3. Click **Apply**. The first build takes about 5–10 minutes.
4. Open the URL Render gives you, e.g. `https://smart-study-planner.onrender.com`.

(Manual alternative: **New + → Web Service**, then pick the repo. Render detects the `Dockerfile`. Choose the **Free** plan and add the environment variable `FLASK_SECRET_KEY` with any long random text.)

## 3. Updating

Every `git push` to `main` redeploys automatically.

## Good to know (free plan)

| Thing | What happens | Fix, if it matters |
| --- | --- | --- |
| Sleeps after ~15 min idle | First visit takes ~1 minute to wake up | Paid instance, or just warn viewers |
| Storage is temporary | Tasks and plans reset on each deploy or restart | Paid persistent disk (set `STUDYPLANNER_DB` to it) or a hosted database |
| 512 MB memory | OCR of a *new* large scanned PDF may run out of memory | Sample syllabus always works (pre-read). Use a bigger instance for heavy OCR |
| Shared data | Every visitor sees the same tasks and plans | Needs user accounts (future upgrade) |

Render's free-plan limits change from time to time. Check render.com/pricing.

## Files used for deployment

- `Dockerfile`: Python 3.12 + RapidOCR, run by gunicorn (1 worker, 4 threads, 300 s timeout)
- `requirements-render.txt`: light dependencies (no PyTorch/EasyOCR)
- `render.yaml`: Render Blueprint
- `.dockerignore`: keeps secrets, venv and local data out of the image
- `sample/`: sample syllabus PDF + its pre-read OCR result
