# Getting a public URL (Live Preview / Deployment URL)

You have two different things, and they need different hosts:

| What | Runs code? | Where it goes |
|---|---|---|
| The **seat allocation engine** (`src/seatalloc/`) | yes, Python | GitHub for the code + a **Python host** for the live demo (`webapp/server.py`) |
| The **recruitment landing page** (`index.html`) | no, static HTML | **GitHub Pages** — free, instant, no server |

> GitHub Pages cannot run Python. If you put this repo on Pages you get a 404 page, because
> nothing there executes `server.py`. That is the single most common mix-up here.

---

## 0. Push the repo to GitHub first (needed either way)

```bash
cd ~/Downloads/Jamia/Lab/SoarJMI/Tasks/soarjmi-seat-allocation
git init -q                       # already done if you downloaded the repo with .git
git add -A
git commit -m "feat: heap-based event seat allocation engine"
git branch -M main
git remote add origin https://github.com/4yushman/seatalloc.git
git push -u origin main
```

Create the empty `seatalloc` repo on GitHub first (github.com → **New repository** → Public →
**do not** tick "Add a README"). If git asks for a password, use a **Personal Access Token**
(GitHub → Settings → Developer settings → Tokens (classic) → scope `repo`) — GitHub does not
accept your account password over the command line any more.

---

## 1. Run it on your own machine (do this before deploying)

```bash
python3 --version                  # need 3.11 or newer
python3 webapp/server.py           # → http://localhost:8000
```

Open <http://localhost:8000> in Firefox/Chrome. Press **▶ Run the scripted demo**, then cancel a
confirmed seat and watch the offer appear. Ctrl+C in the terminal stops it.

If you get `SyntaxError` or `TypeError` about `StrEnum`, your Python is older than 3.11:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y && sudo apt update
sudo apt install python3.13 python3.13-venv -y
python3.13 webapp/server.py
```

---

## 2. Deploy it — Render, free tier (recommended, ~2 minutes, no card)

1. Go to <https://render.com> → **Get Started** → **GitHub** → authorise → sign in.
2. **New +** (top right) → **Blueprint**.
3. Pick your `seatalloc` repo → **Connect**. Render reads `render.yaml` and fills everything in.
4. **Apply** → wait ~1 minute for the first build.
5. Your URL appears at the top of the service page:

   ```
   https://seatalloc-demo.onrender.com
   ```

   That is the **Deployment URL** to put in the submission form.

**Without the blueprint** (if you prefer the manual route): **New + → Web Service** → pick the
repo → Language **Python 3** → Build Command `echo skip` → Start Command
`python webapp/server.py` → Instance type **Free** → **Create Web Service**.

Free-tier behaviour worth knowing before you present:

- The instance **sleeps after ~15 minutes idle**. The next visit takes ~30–60 s to wake up.
- So open your URL once about five minutes before the presentation, and keep the tab open.
- `/healthz` returns `{"ok": true}` — handy to check if it is awake.

---

## 3. Alternative hosts (all free tiers)

| Host | Steps | Notes |
|---|---|---|
| **Railway** — railway.app | New Project → Deploy from GitHub repo | Reads the bundled `Procfile`; add a public domain under Settings → Networking |
| **Fly.io** — fly.io | `fly launch` → `fly deploy` | Needs a `fly.toml`; internal port 8000 |
| **PythonAnywhere** | New web app → Manual → WSGI file | More setup; good if you want it always-on |
| **Replit** | Import from GitHub → Run | Simplest for a one-off demo link |

Whatever host you pick, the two things that matter are identical:

```
start command : python webapp/server.py
port          : read from $PORT automatically — do not hard-code 8000
```

Both are already handled in `webapp/server.py`.

---

## 4. Landing page (the other deliverable) → GitHub Pages

Seconds, and it gives a permanent URL:

1. Create a repo (e.g. `soar-jmi`), put `index.html` in the **root**.
2. **Settings → Pages → Source: Deploy from a branch → Branch: `main` / `root` → Save**.
3. ~1 minute later: `https://4yushman.github.io/soar-jmi/`.

Or, on any repo you already have: **Settings → Pages**, same settings — `index.html` at the repo
root is served as the site.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `Address already in use` | Something is on port 8000: `PORT=8080 python3 webapp/server.py` |
| `python3: command not found` | `sudo apt install python3` (Ubuntu) |
| `ModuleNotFoundError: seatalloc` | Run from the repo root; the server adds `src/` to the path itself |
| Render build fails on `pip install` | Build command must be `echo skip` — there is nothing to install |
| Page loads but buttons do nothing | You opened `webapp/index.html` directly as a `file://` URL. Use the server URL instead |
| Downloads do nothing | Some embedded previews block downloads; open the URL in a normal browser tab |
| Free instance slow on first hit | Cold start after sleeping; open the URL early |

---

## 6. What to paste in the submission

```
GitHub repository : https://github.com/4yushman/seatalloc
Live preview      : https://seatalloc-demo.onrender.com
Run locally       : python webapp/server.py   →   http://localhost:8000
Tests             : python -m pytest -q       (99 tests)
```
