import os, json, base64, asyncio, datetime as dt
from typing import List, Optional
from fastapi import FastAPI
from pydantic import BaseModel
from tenacity import retry, wait_exponential, stop_after_attempt
from openai import AsyncOpenAI
import httpx

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TZ = os.getenv('TZ', 'Europe/Madrid')
PARENT_FOLDER_ID = os.environ.get('DRIVE_PARENT_FOLDER_ID')
SA_INFO = json.loads(os.environ.get('GDRIVE_SA_JSON', '{}'))
DEFAULT_SIZE = os.getenv('DEFAULT_SIZE', '1024x1792')
CONCURRENCY = int(os.getenv('CONCURRENCY', '4'))
STYLE_PRESET_DEFAULT = os.getenv('STYLE_PRESET_DEFAULT', '').strip()
SLACK_WEBHOOK_URL = (os.getenv('SLACK_WEBHOOK_URL') or '').strip()
SLACK_NOTIFY_MODE = (os.getenv('SLACK_NOTIFY_MODE') or 'all').strip()  # all | base_only | errors_only | off

OPENAI_API_KEY = (os.environ.get('OPENAI_API_KEY') or '').strip()
OPENAI_API_KEYS = [k.strip() for k in (os.environ.get('OPENAI_API_KEYS') or '').split(',') if k.strip()]
if not OPENAI_API_KEYS and OPENAI_API_KEY:
    OPENAI_API_KEYS = [OPENAI_API_KEY]

app = FastAPI()

_clients: List[AsyncOpenAI] = [AsyncOpenAI(api_key=k) for k in OPENAI_API_KEYS] if OPENAI_API_KEYS else []
_rr_lock = asyncio.Lock()
_rr_idx = 0

async def get_client() -> AsyncOpenAI:
    global _rr_idx
    if not _clients:
        raise RuntimeError('No OpenAI API key configured (OPENAI_API_KEY or OPENAI_API_KEYS).')
    async with _rr_lock:
        c = _clients[_rr_idx % len(_clients)]
        _rr_idx += 1
        return c

def drive_service():
    creds = service_account.Credentials.from_service_account_info(
        SA_INFO,
        scopes=['https://www.googleapis.com/auth/drive','https://www.googleapis.com/auth/drive.file']
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

def sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        SA_INFO,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)

HEADERS = ['index','prompt','size','status','file_id','web_link','preview','last_error','variants','style']

def find_or_create_sheet_for_date(date_str: str) -> str:
    if not PARENT_FOLDER_ID:
        raise RuntimeError('DRIVE_PARENT_FOLDER_ID missing.')
    d = drive_service()
    q = (f"name = '{date_str}' and mimeType = 'application/vnd.google-apps.spreadsheet' and '") + PARENT_FOLDER_ID + "' in parents and trashed = false"
    r = d.files().list(q=q, fields='files(id,name)').execute()
    if r.get('files'):
        return r['files'][0]['id']
    file_metadata = {'name': date_str,'mimeType': 'application/vnd.google-apps.spreadsheet','parents':[PARENT_FOLDER_ID]}
    f = d.files().create(body=file_metadata, fields='id').execute()
    sid = f['id']
    sh = sheets_service()
    sh.spreadsheets().values().update(spreadsheetId=sid, range='Prompts!A1:J1', valueInputOption='RAW', body={'values':[HEADERS]}).execute()
    sh.spreadsheets().values().update(spreadsheetId=sid, range='Prompts!K1:P1', valueInputOption='RAW', body={'values':[['V1','V2','V3','V4','V5','V6']]}).execute()
    return sid

def list_rows(spreadsheet_id: str) -> List[List[str]]:
    sh = sheets_service()
    r = sh.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range='Prompts!A2:ZZ').execute()
    return r.get('values', [])

def _col_letter(n: int) -> str:
    s = ''
    while n:
        n, r = divmod(n-1, 26)
        s = chr(65+r) + s
    return s

def write_cells(spreadsheet_id: str, start_row: int, values: List[List[str]]):
    sh = sheets_service()
    width = max(len(v) for v in values) if values else 0
    rng = f"Prompts!A{start_row}:{_col_letter(width)}{start_row+len(values)-1}"
    sh.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range=rng, valueInputOption='USER_ENTERED', body={'values': values}).execute()

def write_cell(spreadsheet_id: str, row: int, col: int, value: str):
    sh = sheets_service()
    rng = f"Prompts!{_col_letter(col)}{row}"
    sh.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range=rng, valueInputOption='USER_ENTERED', body={'values': [[value]]}).execute()

def make_file_public(file_id: str) -> str:
    d = drive_service()
    try:
        d.permissions().create(fileId=file_id, body={'type':'anyone','role':'reader'}, fields='id').execute()
    except HttpError as e:
        if getattr(e, 'resp', None) and e.resp.status not in (403, 404):
            raise
    f = d.files().get(fileId=file_id, fields='webViewLink').execute()
    return f['webViewLink']

def upload_png(file_name: str, data: bytes, parent_folder_id: str) -> str:
    from googleapiclient.http import MediaInMemoryUpload
    d = drive_service()
    metadata = {'name': file_name, 'parents':[parent_folder_id]}
    media = MediaInMemoryUpload(data, mimetype='image/png', resumable=False)
    f = d.files().create(body=metadata, media_body=media, fields='id').execute()
    return f['id']

def image_formula(file_id: str) -> str:
    return f'=IMAGE("https://drive.google.com/uc?export=view&id={file_id}")'


# ---------- Slack notify ----------
def sheet_cell_url(spreadsheet_id: str, rownum: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0&range=A{rownum}"

async def slack_post(payload: dict):
    if not SLACK_WEBHOOK_URL or SLACK_NOTIFY_MODE == 'off':
        return
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            await hc.post(SLACK_WEBHOOK_URL, json=payload)
    except Exception:
        pass  # don't crash the pipeline on Slack failures

async def slack_notify_image(spreadsheet_id: str, rownum: int, index: str, file_id: str, prompt: str, is_base: bool):
    if SLACK_NOTIFY_MODE in ('errors_only', 'off'):
        return
    if SLACK_NOTIFY_MODE == 'base_only' and not is_base:
        return
    uc_url = f"https://drive.google.com/uc?export=view&id={file_id}"
    title = f"Image prête — ligne {rownum} (#{index}) {'[base]' if is_base else '[variante]'}"
    text = (prompt or '')[:280]
    payload = {
        "text": title,
        "blocks": [
            {"type":"section","text":{"type":"mrkdwn","text": f"*{title}*\n<{sheet_cell_url(spreadsheet_id,rownum)}|Ouvrir la ligne>"}},
            {"type":"section","text":{"type":"mrkdwn","text": f"```
{text}
```"}},
            {"type":"image","image_url": uc_url, "alt_text":"preview"}
        ]
    }
    await slack_post(payload)

async def slack_notify_error(spreadsheet_id: str, rownum: int, index: str, prompt: str, err: str):
    if SLACK_NOTIFY_MODE in ('off',):
        return
    if SLACK_NOTIFY_MODE not in ('all','errors_only','base_only'):
        return
    title = f"Erreur génération — ligne {rownum} (#{index})"
    text = (prompt or '')[:280]
    payload = {
        "text": title,
        "blocks": [
            {"type":"section","text":{"type":"mrkdwn","text": f"*{title}*\n<{sheet_cell_url(spreadsheet_id,rownum)}|Ouvrir la ligne>"}},
            {"type":"section","text":{"type":"mrkdwn","text": f"Prompt:\n```
{text}
```"}},
            {"type":"section","text":{"type":"mrkdwn","text": f"Erreur:\n```
{(err or '')[:500]}
```"}}
        ]
    }
    await slack_post(payload)


@retry(wait=wait_exponential(min=2, max=20), stop=stop_after_attempt(5))
async def generate_image_b64(prompt: str, size: str) -> bytes:
    client = await get_client()
    img = await client.images.generate(model='gpt-image-1', prompt=prompt, size=size, n=1)
    return base64.b64decode(img.data[0].b64_json)

FIRST_VARIANT_COL = 11  # K

async def append_variant(spreadsheet_id: str, rownum: int, file_id: str):
    sh = sheets_service()
    rng = f'Prompts!K{rownum}:ZZ{rownum}'
    r = sh.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
    row_vals = r.get('values', [[]])
    row_vals = row_vals[0] if row_vals else []
    for i, v in enumerate(row_vals, start=FIRST_VARIANT_COL):
        if v in ('', None):
            target_col = i
            break
    else:
        target_col = FIRST_VARIANT_COL + len(row_vals)
    write_cell(spreadsheet_id, rownum, target_col, image_formula(file_id))

async def process_row(spreadsheet_id: str, parent_folder_id: str, rownum: int, row: List[str], regen_only: bool=False):
    def col(n): return row[n] if n < len(row) else ''
    index  = col(0) or str(rownum-1)
    prompt = col(1)
    size   = col(2) or DEFAULT_SIZE
    status = (col(3) or '').upper()
    variants_s = col(8)  # I
    style_preset = col(9)  # J
    try:
        variants = int(variants_s) if variants_s else 1
    except:
        variants = 1
    # Compose final prompt with optional style preset + global default
    final_prompt = prompt
    preset_parts = []
    if STYLE_PRESET_DEFAULT:
        preset_parts.append(STYLE_PRESET_DEFAULT)
    if style_preset:
        preset_parts.append(style_preset)
    if preset_parts:
        final_prompt = "\n\n".join(preset_parts) + "\n\n" + prompt
    if not prompt:
        return
    if regen_only:
        to_generate = max(1, variants)
        base_update = False
    else:
        if status not in ('', 'PENDING', 'REGEN', 'ERROR'):
            return
        to_generate = max(1, variants)
        base_update = True
    for i in range(to_generate):
        try:
            png = await generate_image_b64(final_prompt, size)
            fname = f"{str(index).zfill(2)}_{i+1}.png" if to_generate > 1 else f"{str(index).zfill(2)}.png"
            file_id = upload_png(fname, png, parent_folder_id)
            web_link = make_file_public(file_id)
            if base_update and i == 0:
                new = [index, prompt, size, 'DONE', file_id, web_link, image_formula(file_id), '', variants_s, style_preset]
                while len(new) < 10:
                    new.append('')
                write_cells(spreadsheet_id, rownum, [new])
            await slack_notify_error(spreadsheet_id, rownum, index, prompt, str(e))
            await append_variant(spreadsheet_id, rownum, file_id)
            await slack_notify_image(spreadsheet_id, rownum, index, file_id, prompt, is_base=(base_update and i == 0))
        except Exception as e:
            new = [index or '', prompt or '', size or '', 'ERROR', '', '', '', str(e)[:500], variants_s or '', style_preset or '']
            while len(new) < 10:
                new.append('')
            write_cells(spreadsheet_id, rownum, [new])
            await slack_notify_error(spreadsheet_id, rownum, index, prompt, str(e))

async def process_sheet(spreadsheet_id: str, parent_folder_id: str, only_rows: Optional[List[int]]=None):
    rows = list_rows(spreadsheet_id)
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = []
    for i, r in enumerate(rows):
        rownum = i + 2
        if only_rows and rownum not in only_rows:
            continue
        regen_only = bool(only_rows)
        async def worker(rn=rownum, row=r, regen=regen_only):
            async with sem:
                await process_row(spreadsheet_id, parent_folder_id, rn, row, regen_only=regen)
        tasks.append(worker())
    if tasks:
        await asyncio.gather(*tasks)

class RunBody(BaseModel):
    date: Optional[str] = None
    spreadsheetId: Optional[str] = None

class RegenBody(BaseModel):
    spreadsheetId: str
    rows: List[int]

@app.get('/health')
def health():
    return {'ok': True}

@app.post('/run')
async def run(body: RunBody):
    if not PARENT_FOLDER_ID:
        raise RuntimeError('DRIVE_PARENT_FOLDER_ID missing.')
    if not OPENAI_API_KEYS:
        raise RuntimeError('OPENAI_API_KEY(S) missing.')
    if body.spreadsheetId:
        sid = body.spreadsheetId
    else:
        date = body.date or dt.datetime.now().astimezone().date().isoformat()
        sid = find_or_create_sheet_for_date(date)
    await process_sheet(sid, PARENT_FOLDER_ID)
    return {'status': 'queued_done', 'spreadsheetId': sid}

@app.post('/regenerate')
async def regenerate(body: RegenBody):
    if not PARENT_FOLDER_ID:
        raise RuntimeError('DRIVE_PARENT_FOLDER_ID missing.')
    if not OPENAI_API_KEYS:
        raise RuntimeError('OPENAI_API_KEY(S) missing.')
    await process_sheet(body.spreadsheetId, PARENT_FOLDER_ID, only_rows=body.rows)
    return {'status': 'queued_done', 'spreadsheetId': body.spreadsheetId, 'rows': body.rows}
