
# Gospel Image Pipeline — Sheets → Nightly Generation → Drive Previews

Batch-generate sacred illustrations overnight from prompts you write in **Google Sheets**.  
Images are uploaded to your **Google Drive** and displayed directly **inside the sheet** via `=IMAGE()`.
Includes a Google Sheets **menu/button** to **re-generate** selected rows.

> Timezone: defaults to `Europe/Madrid`. Endpoints: `/run` (full sheet) and `/regenerate` (selected rows).

---

## 1) What you get

- **FastAPI** service with two endpoints:
  - `POST /run` — reads the sheet (or creates today's `YYYY-MM-DD` sheet in your parent Drive folder), generates all rows with `status in (PENDING, ERROR, REGEN, '')`, uploads images to Drive, and writes preview formulas back to the sheet.
  - `POST /regenerate` — same as above, but only for the **explicit list of row numbers** (e.g. `rows: [2,3,7]`).
- **Google Sheets menu**: “Illustrations → Relancer génération…” to trigger `/regenerate` from inside the sheet.
- Parallel generation (`CONCURRENCY`) with retries & exponential backoff.
- Drive upload + public-view link + in-cell `=IMAGE()` preview.

---

## 2) Google Drive / Sheets setup

1. Create a **Service Account** in Google Cloud and enable **Drive API** and **Sheets API**.
2. Share your **target parent folder** (where the per-day files will live) with the **Service Account email** (Editor).
3. Put the **Service Account JSON** content into Heroku Config Var `GDRIVE_SA_JSON`.
4. Note the parent folder ID as `DRIVE_PARENT_FOLDER_ID` (the string after `folders/` in the URL).

> The app will create (or reuse) a spreadsheet named `<YYYY-MM-DD>` in that parent folder, with a tab `Prompts` and headers.

### Sheet structure (tab `Prompts`)

| Col | Name      | Meaning                                            |
|-----|-----------|----------------------------------------------------|
| A   | index     | 1..30 (string or number)                           |
| B   | prompt    | Your final prompt for the image                    |
| C   | size      | e.g. `1024x1792` (optional, overrides default)     |
| D   | status    | `PENDING` / `DONE` / `ERROR` / `REGEN` / empty     |
| E   | file_id   | Drive file id (filled by the app)                  |
| F   | web_link  | Drive web view link (filled by the app)            |
| G   | preview   | `=IMAGE("https://drive.google.com/uc?export=view&id="&E2)` |
| H   | last_error| Error message if any (filled by the app)           |

You fill **B** (and optionally **C**). Set **D** to `PENDING` (or keep empty) for rows to render.

---

## 3) Deploy to Heroku

```bash
heroku create
heroku buildpacks:add heroku/python
git push heroku main
heroku addons:create scheduler:standard
heroku config:set TZ=Europe/Madrid \
  OPENAI_API_KEY=... \
  DRIVE_PARENT_FOLDER_ID=... \
  GDRIVE_SA_JSON='{...full JSON content...}'
```

Create a nightly Scheduler job (e.g., 01:00 local):
```bash
heroku addons:open scheduler   # then add:  curl -X POST https://<your-app>.herokuapp.com/run
```

Alternatively include date:
```bash
curl -X POST https://<your-app>.herokuapp.com/run \
  -H "Content-Type: application/json" \
  -d '{"date":"2025-09-04"}'
```

---

## 4) Use from Google Sheets (button/menu)

Open the sheet → **Extensions → Apps Script** → paste the content from `apps_script.gs` (in this repo).  
Update `HEROKU_BASE` to your app URL.

It adds a top menu **Illustrations** with:
- **Relancer génération (lignes sélectionnées)** → triggers `/regenerate` for the selected rows
- **Lancer tout le sheet** → triggers `/run`

You can also insert a Drawing/Image and **Assign script** → `regenSelected` to have a big “REGENERATE” button.

---

## 5) Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=... DRIVE_PARENT_FOLDER_ID=... TZ=Europe/Madrid
export GDRIVE_SA_JSON='{...json...}'
uvicorn app:app --reload --port 8000
```

Test:
```bash
curl -X POST http://localhost:8000/run
```

---

## 6) Env Vars

- `OPENAI_API_KEY` — OpenAI API key (Images API).
- `GDRIVE_SA_JSON` — Service Account JSON (full content).
- `DRIVE_PARENT_FOLDER_ID` — Parent Drive folder where daily files live (and where images are uploaded).
- `TZ` — default `Europe/Madrid`.
- `DEFAULT_SIZE` — default image size (e.g., `1024x1792`).
- `CONCURRENCY` — parallelism for image generation (default 4).

---

## 7) Notes

- The app sets each uploaded file to “anyone with the link: reader” to make the `=IMAGE()` fetch reliable. You can remove/modify that if you prefer stricter visibility (but previews may break for some accounts).
- Rows with `DONE` are **not** reprocessed unless you call `/regenerate` with explicit rows or change `status` to `REGEN`/`PENDING`.
- Errors are captured to column H.
- You can horizontally scale concurrency cautiously to balance cost/limits.

---

## 8) Files in this repo

- `app.py` — FastAPI service, Drive/Sheets clients, generation workers.
- `apps_script.gs` — Google Apps Script menu/buttons for Sheets.
- `requirements.txt`, `Procfile`, `runtime.txt` — deploy bits for Heroku.
- `.env.example`, `.gitignore` — convenience.

---

## 9) Security

- Treat the Service Account JSON like a secret. Use Heroku Config Vars (never commit real keys).
- Consider enabling domain-based sharing and IAM scoping per your org policies.

---

## 10) License

MIT — do what you want, at your own risk.


### Variants & History
- New column **I: variants** — set to an integer (e.g., 3) to generate multiple images for that row.
- On first run, the first image fills **E/F/G** and **every** image is appended to history columns **K..** (V1, V2, ...).
- On **Regenerate** from the Sheet menu, the app **does not overwrite** E/F/G — it only appends the new image into the **next empty** history column in **K..**.

### Round-robin API keys
- Provide multiple keys via `OPENAI_API_KEYS="sk-...1,sk-...2"` to shard load across your two paid accounts.


**Global default style**: set `STYLE_PRESET_DEFAULT` in env to prepend a global style to all prompts.


## Slack notifications (optional)
Set a Slack **Incoming Webhook** URL in `SLACK_WEBHOOK_URL` to receive a message **when each image is done** (with a preview and a link to the row).

Env vars:
- `SLACK_WEBHOOK_URL` — your Slack incoming webhook.
- `SLACK_NOTIFY_MODE` — `all` (default, every image), `base_only` (only the first/base image per row), `errors_only`, or `off`.

Each notification includes: row number, index, a snippet of the prompt, a direct preview (Drive public `uc` link), and a deep link to the sheet row.
