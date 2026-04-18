from flask import Flask, render_template, request, send_file, redirect, url_for, after_this_request, flash, Response, jsonify, send_from_directory
from Cryptodome.Cipher import AES
from Cryptodome.Random import get_random_bytes
import os
import sqlite3
from datetime import datetime
import base64
import hashlib
import hmac
import requests as http_requests
import concurrent.futures
import time
import json
import shutil
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import threading
import uuid
import re
import traceback
from urllib.parse import unquote

app = Flask(__name__)
app.secret_key = os.urandom(16)


def log_message(message):
    print(message, flush=True)


FRONTEND_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend_dist')
USER_PREFERENCES = {}
USE_FILEBROWSER_FRONTEND = os.environ.get('USE_FILEBROWSER_FRONTEND', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def frontend_ready():
    return USE_FILEBROWSER_FRONTEND and os.path.exists(os.path.join(FRONTEND_DIST, 'index.html'))


def render_frontend_or_template(template_name):
    if frontend_ready():
        return send_from_directory(FRONTEND_DIST, 'index.html')
    return render_template(template_name)


def frontend_bootstrap_config():
    return {
        'Name': 'Telegram Cloud Storage',
        'BaseURL': '',
        'StaticURL': '',
        'LoginPage': True,
        'NoAuth': False,
        'DisableExternal': True,
        'DisableUsedPercentage': True,
        'Theme': 'dark',
        'Version': 'v1.2.9',
        'Signup': False,
        'ReCaptcha': False,
        'ReCaptchaKey': '',
        'AuthMethod': 'json',
        'LogoutPage': '/logout',
        'EnableThumbs': False,
        'ResizePreview': False,
        'EnableExec': False,
        'CSS': False,
        'Color': '',
        'TusSettings': None,
        'HideLoginButton': False,
    }

# All persistent data stored under /app/data (mapped to host via volume)
DATA_DIR = os.environ.get('DATA_DIR', 'data')
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_FILE = os.path.join(DATA_DIR, 'file_data.db')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TEMP_UPLOAD = os.path.join(DATA_DIR, 'temp_upload')
TEMP_CHUNKS = os.path.join(DATA_DIR, 'temp_chunks')
TEMP_DOWNLOAD = os.path.join(DATA_DIR, 'temp_download')

# Ensure temp dirs exist
for d in [TEMP_UPLOAD, TEMP_CHUNKS, TEMP_DOWNLOAD]:
    os.makedirs(d, exist_ok=True)

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)


@login_manager.unauthorized_handler
def unauthorized_handler():
    if request.path.startswith('/api/'):
        return api_error('Unauthorized', 401)
    return redirect(url_for('login', next=request.path))


@app.route('/api/frontend-config.js')
def frontend_config_js():
    config = json.dumps(frontend_bootstrap_config())
    script = f"window.FileBrowser = {config};"
    return Response(script, mimetype='application/javascript')


class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role


@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    user_data = cursor.fetchone()
    conn.close()
    if user_data:
        return User(user_data[0], user_data[1], user_data[2])
    return None


# --- Database ---
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT UNIQUE,
                      password TEXT,
                      role TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS folders
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      name TEXT,
                      parent_id INTEGER,
                      owner_id INTEGER,
                      FOREIGN KEY(parent_id) REFERENCES folders(id),
                      FOREIGN KEY(owner_id) REFERENCES users(id))''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS files
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      file_name TEXT,
                      chunk_list TEXT,
                      message_ids TEXT,
                      key_hex TEXT,
                      file_size INTEGER,
                      upload_date TEXT,
                      folder_id INTEGER,
                      owner_id INTEGER,
                      status TEXT DEFAULT 'Ready',
                      error_message TEXT,
                      job_id TEXT,
                      public_token TEXT,
                      FOREIGN KEY(folder_id) REFERENCES folders(id),
                      FOREIGN KEY(owner_id) REFERENCES users(id))''')

    # Schema migration
    cursor.execute("PRAGMA table_info(files)")
    columns = [col[1] for col in cursor.fetchall()]
    for col, default in [('message_ids', 'TEXT'), ('folder_id', 'INTEGER'), ('owner_id', 'INTEGER'),
                         ('status', "TEXT DEFAULT 'Ready'"), ('error_message', 'TEXT'),
                         ('job_id', 'TEXT'), ('public_token', 'TEXT')]:
        if col not in columns:
            cursor.execute(f"ALTER TABLE files ADD COLUMN {col} {default}")

    # Default admin account
    cursor.execute("SELECT id FROM users WHERE username = ?", ('admin',))
    if not cursor.fetchone():
        hashed_pw = generate_password_hash('admin')
        cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", ('admin', hashed_pw, 'admin'))

    conn.commit()
    conn.close()


init_db()


# --- Helpers ---
def convert_bytes(byte_size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if byte_size < 1024.0:
            break
        byte_size /= 1024.0
    return f"{byte_size:.2f} {unit}"


def file_extension(filename):
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def serialize_folder(row):
    return {
        'id': row[0],
        'name': row[1]
    }


def serialize_file(result):
    f_id, f_name, f_size, f_list, f_status, f_error_message, f_job_id, pub_token = result
    return {
        'id': f_id,
        'file_name': f_name,
        'formatted_size': convert_bytes(f_size),
        'size_bytes': f_size,
        'chunk_amount': len(f_list.split(', ')) if f_list else 0,
        'status': f_status,
        'job_id': f_job_id,
        'public_token': pub_token,
        'error_message': f_error_message,
        'extension': file_extension(f_name)
    }


def api_error(message, status=400):
    return jsonify({'ok': False, 'message': message}), status


def api_success(payload=None, status=200):
    body = {'ok': True}
    if payload:
        body.update(payload)
    return jsonify(body), status


def fb_error(message, status=400, headers=None):
    response = Response(str(message), status=status, mimetype='text/plain; charset=utf-8')
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def fb_json(payload, status=200, headers=None):
    response = jsonify(payload)
    response.status_code = status
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


def filebrowser_permissions(role):
    return {
        'admin': False,
        'copy': False,
        'create': True,
        'delete': True,
        'download': True,
        'execute': False,
        'modify': True,
        'move': True,
        'rename': True,
        'share': False,
        'shell': False,
        'upload': True,
    }


def default_user_preferences():
    return {
        'viewMode': 'mosaic',
        'sorting': {'by': 'name', 'asc': True},
        'locale': 'vi',
        'dateFormat': False,
        'singleClick': False,
        'hideDotfiles': False,
        'redirectAfterCopyMove': False,
        'aceEditorTheme': 'github',
    }


def get_user_preferences(user_id):
    prefs = USER_PREFERENCES.get(user_id)
    if not prefs:
        prefs = default_user_preferences()
        USER_PREFERENCES[user_id] = prefs
    return prefs


def filebrowser_user_from_row(row):
    prefs = get_user_preferences(row[0])
    return {
        'id': row[0],
        'username': row[1],
        'password': '',
        'scope': '/',
        'locale': prefs['locale'],
        'perm': filebrowser_permissions(row[2]),
        'commands': [],
        'rules': [],
        'lockPassword': False,
        'hideDotfiles': prefs['hideDotfiles'],
        'singleClick': prefs['singleClick'],
        'redirectAfterCopyMove': prefs['redirectAfterCopyMove'],
        'dateFormat': prefs['dateFormat'],
        'viewMode': prefs['viewMode'],
        'sorting': prefs['sorting'],
        'aceEditorTheme': prefs['aceEditorTheme'],
    }


def fetch_user_row(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def base64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def base64url_decode(value):
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode('ascii'))


def issue_api_token(user_row, expiry_seconds=86400):
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {
        'exp': int(time.time()) + expiry_seconds,
        'user': filebrowser_user_from_row(user_row),
    }
    header_b64 = base64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    payload_b64 = base64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{header_b64}.{payload_b64}".encode('ascii')
    signature = hmac.new(app.secret_key, signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{base64url_encode(signature)}"


def verify_api_token(token):
    try:
        header_b64, payload_b64, signature_b64 = token.split('.')
        signing_input = f"{header_b64}.{payload_b64}".encode('ascii')
        expected_sig = hmac.new(app.secret_key, signing_input, hashlib.sha256).digest()
        actual_sig = base64url_decode(signature_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(base64url_decode(payload_b64))
        if int(payload.get('exp', 0)) <= int(time.time()):
            return None
        user = payload.get('user') or {}
        user_id = user.get('id')
        if not user_id:
            return None
        row = fetch_user_row(user_id)
        return row
    except Exception:
        return None


def api_auth_row():
    if current_user.is_authenticated:
        row = fetch_user_row(current_user.id)
        if row:
            return row
    token = request.headers.get('X-Auth', '').strip() or request.cookies.get('auth', '').strip()
    if token:
        return verify_api_token(token)
    return None


def require_api_auth():
    row = api_auth_row()
    if not row:
        return None, fb_error('Unauthorized', 401)
    return row, None


def normalize_virtual_path(resource_path=''):
    clean = unquote(resource_path or '').strip()
    if not clean or clean == '/':
        return '/'
    clean = '/' + clean.strip('/')
    if request.path.endswith('/') and not clean.endswith('/'):
        clean += '/'
    return clean


def split_virtual_path(resource_path=''):
    normalized = normalize_virtual_path(resource_path)
    if normalized == '/':
        return '', ''
    stripped = normalized.strip('/')
    parts = stripped.split('/')
    if len(parts) == 1:
        return '', parts[0]
    return '/'.join(parts[:-1]), parts[-1]


def classify_resource_type(name, is_dir):
    if is_dir:
        return 'dir'
    ext = os.path.splitext(name)[1].lower()
    if ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'):
        return 'image'
    if ext in ('.mp4', '.mkv', '.avi', '.mov', '.webm'):
        return 'video'
    if ext in ('.mp3', '.wav', '.ogg', '.flac', '.aac'):
        return 'audio'
    if ext == '.pdf':
        return 'pdf'
    if ext in (
        '.txt', '.md', '.markdown', '.json', '.yaml', '.yml', '.csv', '.log',
        '.ini', '.toml', '.xml', '.py', '.js', '.ts', '.tsx', '.jsx',
        '.html', '.htm', '.css', '.scss', '.sass', '.less', '.sh', '.env',
        '.sql', '.conf', '.cfg',
    ):
        return 'text'
    return 'blob'


def is_text_like_file(name):
    return classify_resource_type(name, False) == 'text'


def numerical_sort_key(filename):
    match = re.search(r'(?:\.chunk_|chunk_)(\d+)\.enc$', filename)
    return int(match.group(1)) if match else 0


def is_stopped(file_id):
    if not file_id:
        return False
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM files WHERE id = ?", (file_id,))
        result = cursor.fetchone()
        conn.close()
        return result and result[0] == 'Stopped'
    except:
        return False


def get_all_user_folders(user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM folders WHERE owner_id = ?", (user_id,))
    folders = [serialize_folder(r) for r in cursor.fetchall()]
    conn.close()
    return folders


def folder_segments_from_id(folder_id, user_id=None):
    if not folder_id:
        return []
    if user_id is None:
        user_id = current_user.id
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    chain = []
    current_id = folder_id
    while current_id:
        cursor.execute("SELECT id, name, parent_id FROM folders WHERE id = ? AND owner_id = ?", (current_id, user_id))
        row = cursor.fetchone()
        if not row:
            break
        chain.append({'id': row[0], 'name': row[1]})
        current_id = row[2]
    conn.close()
    return list(reversed(chain))


def folder_path_from_id(folder_id, user_id=None):
    segments = folder_segments_from_id(folder_id, user_id)
    return '/'.join(segment['name'] for segment in segments)


def resolve_folder_path(folder_path):
    if not folder_path:
        return None
    clean_path = folder_path.strip('/')
    if not clean_path:
        return None

    current_parent = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    for raw_segment in [segment for segment in clean_path.split('/') if segment]:
        cursor.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS ? AND owner_id = ?",
            (raw_segment, current_parent, current_user.id),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        current_parent = row[0]
    conn.close()
    return current_parent


def resolve_folder_path_for_user(folder_path, user_id):
    if not folder_path:
        return None
    clean_path = folder_path.strip('/')
    if not clean_path:
        return None

    current_parent = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    for raw_segment in [segment for segment in clean_path.split('/') if segment]:
        cursor.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS ? AND owner_id = ?",
            (raw_segment, current_parent, user_id),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        current_parent = row[0]
    conn.close()
    return current_parent


def ensure_folder_path_for_user(folder_path, user_id):
    normalized = normalize_virtual_path(folder_path)
    if normalized == '/':
        return None, None

    segments = [segment for segment in normalized.strip('/').split('/') if segment]
    current_parent = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute('BEGIN IMMEDIATE')
        for raw_segment in segments:
            cursor.execute(
                "SELECT id FROM files WHERE file_name = ? AND folder_id IS ? AND owner_id = ?",
                (raw_segment, current_parent, user_id),
            )
            if cursor.fetchone():
                conn.rollback()
                return None, 'Conflict'

            cursor.execute(
                "SELECT id FROM folders WHERE name = ? AND parent_id IS ? AND owner_id = ? ORDER BY id LIMIT 1",
                (raw_segment, current_parent, user_id),
            )
            row = cursor.fetchone()
            if row:
                current_parent = row[0]
                continue

            cursor.execute(
                "INSERT INTO folders (name, parent_id, owner_id) VALUES (?, ?, ?)",
                (raw_segment, current_parent, user_id),
            )
            current_parent = cursor.lastrowid

        conn.commit()
        return current_parent, None
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_virtual_path(parent_path, name, is_dir=False):
    if not parent_path or parent_path == '/':
        path = '/' + name.strip('/')
    else:
        path = '/' + parent_path.strip('/') + '/' + name.strip('/')
    if is_dir and not path.endswith('/'):
        path += '/'
    return path


def find_file_by_virtual_path(resource_path, user_id):
    folder_path, file_name = split_virtual_path(resource_path)
    folder_id = resolve_folder_path_for_user(folder_path, user_id)
    if folder_path and folder_id is None:
        return None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, file_name, file_size, chunk_list, status, error_message, job_id, public_token, key_hex, upload_date, folder_id
           FROM files WHERE file_name = ? AND folder_id IS ? AND owner_id = ?""",
        (file_name, folder_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def directory_parent_path(folder_id, user_id):
    if not folder_id:
        return '/'
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT parent_id FROM folders WHERE id = ? AND owner_id = ?", (folder_id, user_id))
    row = cursor.fetchone()
    conn.close()
    return '/' + folder_path_from_id(row[0], user_id).strip('/') + ('/' if row and row[0] else '')


def serialize_fb_item(row, parent_path, is_dir):
    if is_dir:
        item_id, name = row[0], row[1]
        modified = now_iso()
        size = 0
        path = build_virtual_path(parent_path, name, True)
        extension = ''
        item_type = 'dir'
    else:
        item_id, name, size, upload_date = row[0], row[1], row[2], row[3]
        modified = datetime.strptime(upload_date, '%Y-%m-%d %H:%M:%S').isoformat() + 'Z' if upload_date else now_iso()
        path = build_virtual_path(parent_path, name, False)
        extension = os.path.splitext(name)[1].lower()
        item_type = classify_resource_type(name, False)
    return {
        'index': 0,
        'name': name,
        'path': path,
        'size': size,
        'extension': extension,
        'modified': modified,
        'mode': 0,
        'isDir': is_dir,
        'isSymlink': False,
        'type': item_type,
        'url': '',
    }


def build_directory_resource(folder_id, user_row):
    user_id = user_row[0]
    parent_path = folder_path_from_id(folder_id, user_id)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM folders WHERE parent_id IS ? AND owner_id = ?", (folder_id, user_id))
    folders = [serialize_fb_item(row, parent_path, True) for row in cursor.fetchall()]
    cursor.execute("SELECT id, file_name, file_size, upload_date FROM files WHERE folder_id IS ? AND owner_id = ?", (folder_id, user_id))
    files = [serialize_fb_item(row, parent_path, False) for row in cursor.fetchall()]
    conn.close()

    prefs = get_user_preferences(user_id)
    by = prefs['sorting']['by']
    asc = prefs['sorting']['asc']

    def sort_key(item):
        if by == 'size':
            return item['size']
        if by == 'modified':
            return item['modified']
        return item['name'].lower()

    folders = sorted(folders, key=sort_key, reverse=not asc)
    files = sorted(files, key=sort_key, reverse=not asc)
    items = folders + files
    for index, item in enumerate(items):
        item['index'] = index

    folder_name = parent_path.split('/')[-1] if parent_path else ''
    return {
        'path': '/' + parent_path.strip('/') + ('/' if parent_path else ''),
        'name': folder_name,
        'size': 0,
        'extension': '',
        'modified': now_iso(),
        'mode': 0,
        'isDir': True,
        'isSymlink': False,
        'type': 'dir',
        'items': items,
        'numDirs': len(folders),
        'numFiles': len(files),
        'sorting': prefs['sorting'],
    }


def build_file_resource(file_row, user_row):
    file_id, file_name, file_size, chunk_list, status, error_message, job_id, public_token, key_hex, upload_date, folder_id = file_row
    parent_path = folder_path_from_id(folder_id, user_row[0])
    ext = os.path.splitext(file_name)[1].lower()
    modified = datetime.strptime(upload_date, '%Y-%m-%d %H:%M:%S').isoformat() + 'Z' if upload_date else now_iso()
    return {
        'path': build_virtual_path(parent_path, file_name, False),
        'name': file_name,
        'size': file_size,
        'extension': ext,
        'modified': modified,
        'mode': 0,
        'isDir': False,
        'isSymlink': False,
        'type': classify_resource_type(file_name, False),
    }


class UploadedBlob:
    def __init__(self, filename, source_path):
        self.filename = filename
        self.source_path = source_path

    def save(self, destination):
        shutil.move(self.source_path, destination)


class UploadedFileAdapter:
    def __init__(self, file_storage, filename):
        self.file_storage = file_storage
        self.filename = filename

    def save(self, destination):
        self.file_storage.save(destination)


def write_temp_blob(filename, content):
    os.makedirs(TEMP_UPLOAD, exist_ok=True)
    temp_input = os.path.join(TEMP_UPLOAD, f"raw_{uuid.uuid4().hex}_{filename}")
    with open(temp_input, 'wb') as target:
        target.write(content)
    return UploadedBlob(filename, temp_input)


def fetch_file_storage_by_id(file_id, user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, file_name, chunk_list, message_ids, key_hex, file_size, upload_date, folder_id, owner_id
           FROM files WHERE id = ? AND owner_id = ?""",
        (file_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def fetch_file_storage_by_virtual_path(resource_path, user_id):
    file_row = find_file_by_virtual_path(resource_path, user_id)
    if not file_row:
        return None
    return fetch_file_storage_by_id(file_row[0], user_id)


def delete_telegram_messages(message_ids_str):
    if not message_ids_str:
        return
    for msg_id in str(message_ids_str).split(', '):
        if not msg_id or msg_id == 'EMPTY':
            continue
        try:
            url = f"http://bot-api:8081/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
            http_requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'message_id': msg_id}, timeout=30)
        except Exception as e:
            log_message(f"Failed to delete message {msg_id}: {e}")


def delete_file_record(file_id, user_id):
    file_row = fetch_file_storage_by_id(file_id, user_id)
    if not file_row:
        return False
    delete_telegram_messages(file_row[3])
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files WHERE id = ? AND owner_id = ?", (file_id, user_id))
    conn.commit()
    conn.close()
    return True


def collect_descendant_folder_ids(folder_id, user_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    queue = [folder_id]
    folder_ids = []

    while queue:
        current_id = queue.pop(0)
        folder_ids.append(current_id)
        cursor.execute("SELECT id FROM folders WHERE parent_id = ? AND owner_id = ?", (current_id, user_id))
        queue.extend(row[0] for row in cursor.fetchall())

    conn.close()
    return folder_ids


def delete_folder_tree(folder_id, user_id):
    folder_ids = collect_descendant_folder_ids(folder_id, user_id)
    if not folder_ids:
        return False

    placeholders = ','.join('?' for _ in folder_ids)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT id FROM files WHERE owner_id = ? AND folder_id IN ({placeholders})",
        [user_id, *folder_ids],
    )
    file_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    for file_id in file_ids:
        delete_file_record(file_id, user_id)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute(
        f"DELETE FROM folders WHERE owner_id = ? AND id IN ({placeholders})",
        [user_id, *folder_ids],
    )
    conn.commit()
    conn.close()
    return True


def get_breadcrumbs(folder_id):
    breadcrumbs = [{'id': None, 'name': 'files'}]
    if not folder_id:
        return breadcrumbs

    breadcrumbs.extend(folder_segments_from_id(folder_id))
    return breadcrumbs


def get_directory_payload(folder_id):
    files_info, folders_info, current_folder_name, parent_folder_id = fetch_directory_contents(folder_id)
    return {
        'folder_id': folder_id,
        'current_folder_name': current_folder_name,
        'current_path': folder_path_from_id(folder_id),
        'parent_folder_id': parent_folder_id,
        'breadcrumbs': get_breadcrumbs(folder_id),
        'folders': folders_info,
        'files': files_info,
        'all_user_folders': get_all_user_folders(current_user.id)
    }


def queue_upload_for_user(file_storage, folder_id, user_id):
    os.makedirs(TEMP_UPLOAD, exist_ok=True)
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(TEMP_UPLOAD, job_id)
    os.makedirs(job_dir, exist_ok=True)
    file_path = os.path.join(job_dir, file_storage.filename)
    file_storage.save(file_path)

    file_size = os.path.getsize(file_path)
    upload_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""INSERT INTO files
                      (file_name, chunk_list, message_ids, key_hex, file_size, upload_date, folder_id, owner_id, status, job_id)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (file_storage.filename, '', '', '', file_size, upload_date, folder_id, user_id, 'Processing', job_id))
    file_id = cursor.lastrowid
    conn.commit()
    conn.close()

    thread = threading.Thread(target=process_file_background,
                              args=(file_id, file_path, folder_id, user_id, job_id))
    thread.start()
    return file_id, job_id


def queue_upload(file_storage, folder_id):
    return queue_upload_for_user(file_storage, folder_id, current_user.id)


# --- Auth Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    init_db()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, password, role FROM users WHERE username = ?", (username,))
        user_data = cursor.fetchone()
        conn.close()
        if user_data and check_password_hash(user_data[2], password):
            user = User(user_data[0], user_data[1], user_data[3])
            login_user(user)
            if frontend_ready():
                next_url = request.args.get('next') or url_for('files_frontend')
                return redirect(next_url)
            return redirect(url_for('index'))
        flash('Invalid username or password', 'error')
    return render_frontend_or_template('login.html')


@app.route('/logout')
def logout():
    if current_user.is_authenticated:
        logout_user()
    return redirect(url_for('login'))


@app.route('/files')
@app.route('/files/')
@app.route('/files/<path:folder_path>')
def files_frontend(folder_path=''):
    if frontend_ready():
        return send_from_directory(FRONTEND_DIST, 'index.html')
    if not current_user.is_authenticated:
        return redirect(url_for('login', next=request.path))
    folder_id = resolve_folder_path(folder_path)
    if folder_path and folder_id is None:
        return "Folder not found", 404
    return redirect(url_for('index', folder_id=folder_id) if folder_id else url_for('index'))


@app.route('/api/session', methods=['GET'])
def api_get_session():
    if not current_user.is_authenticated:
        return api_success({'authenticated': False})
    return api_success({
        'authenticated': True,
        'user': {
            'id': current_user.id,
            'username': current_user.username,
            'role': current_user.role
        }
    })


@app.route('/api/session', methods=['POST'])
def api_login():
    init_db()
    payload = request.get_json(silent=True) or request.form
    username = payload.get('username', '').strip()
    password = payload.get('password', '')
    if not username or not password:
        return api_error('Thiếu tên đăng nhập hoặc mật khẩu', 400)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, role FROM users WHERE username = ?", (username,))
    user_data = cursor.fetchone()
    conn.close()

    if not user_data or not check_password_hash(user_data[2], password):
        return api_error('Tên đăng nhập hoặc mật khẩu không đúng', 401)

    user = User(user_data[0], user_data[1], user_data[3])
    login_user(user)
    return api_success({
        'authenticated': True,
        'user': {
            'id': user.id,
            'username': user.username,
            'role': user.role
        }
    })


@app.route('/api/session', methods=['DELETE'])
@login_required
def api_logout():
    logout_user()
    return api_success({'authenticated': False})


@app.route('/api/login', methods=['POST'])
def filebrowser_login():
    init_db()
    payload = request.get_json(silent=True) or {}
    username = (payload.get('username') or '').strip()
    password = payload.get('password') or ''
    if not username or not password:
        return fb_error('Missing credentials', 400)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password, role FROM users WHERE username = ?", (username,))
    user_data = cursor.fetchone()
    conn.close()
    if not user_data or not check_password_hash(user_data[2], password):
        return fb_error('Wrong credentials', 403)

    user = User(user_data[0], user_data[1], user_data[3])
    login_user(user)
    token = issue_api_token((user_data[0], user_data[1], user_data[3]))
    return Response(token, status=200, mimetype='text/plain; charset=utf-8')


@app.route('/api/renew', methods=['POST'])
def filebrowser_renew():
    row, error = require_api_auth()
    if error:
        return error
    user = User(row[0], row[1], row[2])
    login_user(user)
    token = issue_api_token(row)
    return Response(token, status=200, mimetype='text/plain; charset=utf-8')


@app.route('/api/signup', methods=['POST'])
def filebrowser_signup():
    return fb_error('Signup disabled', 403)


@app.route('/api/browse', methods=['GET'])
@app.route('/api/browse/<int:folder_id>', methods=['GET'])
@login_required
def api_browse(folder_id=None):
    return api_success(get_directory_payload(folder_id))


@app.route('/api/browse-path', methods=['GET'])
@app.route('/api/browse-path/<path:folder_path>', methods=['GET'])
@login_required
def api_browse_path(folder_path=''):
    folder_id = resolve_folder_path(folder_path)
    if folder_path and folder_id is None:
        return api_error('Không tìm thấy thư mục', 404)
    return api_success(get_directory_payload(folder_id))


@app.route('/api/resources', methods=['GET', 'POST'])
@app.route('/api/resources/', methods=['GET', 'POST'])
@app.route('/api/resources/<path:resource_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/api/resources/<path:resource_path>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/api/resources<path:resource_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def filebrowser_resources(resource_path=''):
    user_row, error = require_api_auth()
    if error:
        return error

    normalized = normalize_virtual_path(resource_path)
    user_id = user_row[0]
    overwrite = (request.args.get('override') or 'false').lower() == 'true'

    if request.method == 'GET':
        if normalized.endswith('/'):
            folder_id = resolve_folder_path_for_user(normalized, user_id)
            if normalized != '/' and folder_id is None:
                return fb_error('Not found', 404)
            return fb_json(build_directory_resource(folder_id, user_row))

        file_row = find_file_by_virtual_path(normalized, user_id)
        if not file_row:
            return fb_error('Not found', 404)

        checksum_algo = (request.args.get('checksum') or '').lower()
        if checksum_algo:
            if checksum_algo not in ('md5', 'sha1', 'sha256', 'sha512'):
                return fb_error('Unsupported checksum', 400)
            storage_row = fetch_file_storage_by_id(file_row[0], user_id)
            try:
                content = read_decrypted_content(
                    storage_row[1],
                    storage_row[2].split(', ') if storage_row[2] else [],
                    storage_row[4],
                )
            except Exception as exc:
                log_message(f"Failed to checksum file {normalized}: {exc}")
                return fb_error('Failed to read file', 500)
            return fb_json({'checksums': {checksum_algo: hashlib.new(checksum_algo, content).hexdigest()}})

        if is_text_like_file(file_row[1]):
            storage_row = fetch_file_storage_by_id(file_row[0], user_id)
            try:
                content = read_decrypted_content(
                    storage_row[1],
                    storage_row[2].split(', ') if storage_row[2] else [],
                    storage_row[4],
                )
            except FileNotFoundError as exc:
                return fb_error(str(exc), 404)
            except Exception as exc:
                log_message(f"Failed to read text file {normalized}: {exc}")
                return fb_error('Failed to read file', 500)
            return Response(content, status=200, mimetype='application/octet-stream')

        return fb_json(build_file_resource(file_row, user_row))

    if request.method in ('POST', 'PUT'):
        if normalized.endswith('/'):
            if request.method == 'PUT':
                return fb_error('Cannot write directory content', 400)

            folder_id, ensure_error = ensure_folder_path_for_user(normalized, user_id)
            if ensure_error:
                return fb_error(ensure_error, 409)
            if folder_id is None and normalized != '/':
                return fb_error('Failed to create folder', 500)
            return Response('', status=200)

        parent_path, file_name = split_virtual_path(normalized)
        folder_id, ensure_error = ensure_folder_path_for_user(parent_path, user_id)
        if ensure_error:
            return fb_error(ensure_error, 409)

        existing_file = find_file_by_virtual_path(normalized, user_id)
        if existing_file and request.method == 'POST' and not overwrite:
            return fb_error('Conflict', 409)
        if existing_file:
            delete_file_record(existing_file[0], user_id)

        upload_blob = write_temp_blob(file_name, request.get_data() or b'')
        file_id, _job_id = queue_upload_for_user(upload_blob, folder_id, user_id)
        return Response(str(file_id), status=200, mimetype='text/plain; charset=utf-8')

    if request.method == 'DELETE':
        if normalized.endswith('/'):
            folder_id = resolve_folder_path_for_user(normalized, user_id)
            if normalized != '/' and folder_id is None:
                return fb_error('Not found', 404)
            if folder_id is None:
                return fb_error('Cannot delete root', 400)
            delete_folder_tree(folder_id, user_id)
            return Response('', status=200)

        file_row = find_file_by_virtual_path(normalized, user_id)
        if not file_row:
            return fb_error('Not found', 404)
        delete_file_record(file_row[0], user_id)
        return Response('', status=200)

    action = request.args.get('action', '')
    destination = unquote(request.args.get('destination', ''))
    if action == 'copy':
        return fb_error('Copy unsupported', 400)
    if action != 'rename' or not destination:
        return fb_error('Unsupported action', 400)

    destination = normalize_virtual_path(destination)
    dest_parent_path, dest_name = split_virtual_path(destination)
    dest_parent_id = resolve_folder_path_for_user(dest_parent_path, user_id)
    if dest_parent_path and dest_parent_id is None:
        return fb_error('Destination not found', 404)

    if normalized.endswith('/'):
        folder_id = resolve_folder_path_for_user(normalized, user_id)
        if folder_id is None:
            return fb_error('Not found', 404)
        existing_dest = resolve_folder_path_for_user(destination, user_id)
        if existing_dest is not None and existing_dest != folder_id:
            return fb_error('Conflict', 409)
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE folders SET name = ?, parent_id = ? WHERE id = ? AND owner_id = ?", (dest_name, dest_parent_id, folder_id, user_id))
        conn.commit()
        conn.close()
        return Response('', status=200)

    file_row = find_file_by_virtual_path(normalized, user_id)
    if not file_row:
        return fb_error('Not found', 404)
    existing_dest = find_file_by_virtual_path(destination, user_id)
    if existing_dest and existing_dest[0] != file_row[0]:
        return fb_error('Conflict', 409)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE files SET file_name = ?, folder_id = ? WHERE id = ? AND owner_id = ?", (dest_name, dest_parent_id, file_row[0], user_id))
    conn.commit()
    conn.close()
    return Response('', status=200)


@app.route('/api/raw', methods=['GET'])
@app.route('/api/raw/', methods=['GET'])
@app.route('/api/raw/<path:resource_path>', methods=['GET'])
@app.route('/api/raw<path:resource_path>', methods=['GET'])
@app.route('/raw', methods=['GET'])
@app.route('/raw/', methods=['GET'])
@app.route('/raw/<path:resource_path>', methods=['GET'])
@app.route('/raw<path:resource_path>', methods=['GET'])
def filebrowser_raw(resource_path=''):
    user_row, error = require_api_auth()
    if error:
        return error
    if request.args.get('files'):
        return fb_error('Archive download is not supported yet', 400)

    storage_row = fetch_file_storage_by_virtual_path(normalize_virtual_path(resource_path), user_row[0])
    if not storage_row:
        return fb_error('Not found', 404)

    inline = (request.args.get('inline') or 'false').lower() == 'true'
    return process_download(
        storage_row[1],
        storage_row[2].split(', ') if storage_row[2] else [],
        storage_row[3].split(', ') if storage_row[3] else [],
        storage_row[4],
        file_id=storage_row[0],
        as_attachment=not inline,
    )


@app.route('/api/usage', methods=['GET'])
@app.route('/api/usage/', methods=['GET'])
@app.route('/api/usage/<path:resource_path>', methods=['GET'])
@app.route('/api/usage<path:resource_path>', methods=['GET'])
@app.route('/usage', methods=['GET'])
@app.route('/usage/', methods=['GET'])
@app.route('/usage/<path:resource_path>', methods=['GET'])
@app.route('/usage<path:resource_path>', methods=['GET'])
def filebrowser_usage(resource_path=''):
    user_row, error = require_api_auth()
    if error:
        return error
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(SUM(file_size), 0) FROM files WHERE owner_id = ?", (user_row[0],))
    used = cursor.fetchone()[0] or 0
    conn.close()
    total = max(used, 1)
    return fb_json({'used': used, 'total': total})


@app.route('/api/resources/folder-path', methods=['POST'])
@app.route('/resources/folder-path', methods=['POST'])
def filebrowser_create_folder_by_path():
    user_row, error = require_api_auth()
    if error:
        return error

    payload = request.get_json(silent=True) or request.form
    target_path = (payload.get('path') or '').strip()
    if not target_path:
        return fb_error('Path required', 400)

    normalized = normalize_virtual_path(target_path)
    if not normalized.endswith('/'):
        normalized += '/'

    folder_id, ensure_error = ensure_folder_path_for_user(normalized, user_row[0])
    if ensure_error:
        return fb_error(ensure_error, 409)

    return Response(str(folder_id or ''), status=200, mimetype='text/plain; charset=utf-8')


@app.route('/api/resources/file-path', methods=['POST'])
@app.route('/resources/file-path', methods=['POST'])
def filebrowser_upload_file_by_path():
    user_row, error = require_api_auth()
    if error:
        return error

    if 'file' not in request.files:
        return fb_error('File required', 400)

    target_path = (request.form.get('path') or '').strip()
    if not target_path:
        return fb_error('Path required', 400)

    normalized = normalize_virtual_path(target_path)
    if normalized.endswith('/'):
        return fb_error('Invalid file path', 400)

    overwrite = (request.form.get('override') or 'false').lower() == 'true'
    parent_path, file_name = split_virtual_path(normalized)
    folder_id, ensure_error = ensure_folder_path_for_user(parent_path, user_row[0])
    if ensure_error:
        return fb_error(ensure_error, 409)

    existing_file = find_file_by_virtual_path(normalized, user_row[0])
    if existing_file and not overwrite:
        return fb_error('Conflict', 409)
    if existing_file:
        delete_file_record(existing_file[0], user_row[0])

    upload_file = request.files['file']
    adapted_file = UploadedFileAdapter(upload_file, file_name)
    file_id, _job_id = queue_upload_for_user(adapted_file, folder_id, user_row[0])
    return Response(str(file_id), status=200, mimetype='text/plain; charset=utf-8')


@app.route('/resources', methods=['GET', 'POST'])
@app.route('/resources/', methods=['GET', 'POST'])
@app.route('/resources/<path:resource_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/resources/<path:resource_path>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@app.route('/resources<path:resource_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def filebrowser_resources_alias(resource_path=''):
    return filebrowser_resources(resource_path)


def _resource_suffix_from_path(prefix):
    suffix = request.path[len(prefix):]
    return suffix.lstrip('/')


def _matches_api_prefix(prefix):
    path = request.path or ''
    return path == prefix or path.startswith(prefix + '/')


@app.errorhandler(404)
def api_route_fallback(error):
    if _matches_api_prefix('/api/resources/file-path') or _matches_api_prefix('/resources/file-path'):
        return filebrowser_upload_file_by_path()

    if _matches_api_prefix('/api/resources/folder-path') or _matches_api_prefix('/resources/folder-path'):
        return filebrowser_create_folder_by_path()

    if _matches_api_prefix('/api/resources'):
        return filebrowser_resources(_resource_suffix_from_path('/api/resources'))

    if _matches_api_prefix('/resources'):
        return filebrowser_resources(_resource_suffix_from_path('/resources'))

    if _matches_api_prefix('/api/raw'):
        return filebrowser_raw(_resource_suffix_from_path('/api/raw'))

    if _matches_api_prefix('/raw'):
        return filebrowser_raw(_resource_suffix_from_path('/raw'))

    if _matches_api_prefix('/api/usage'):
        return filebrowser_usage(_resource_suffix_from_path('/api/usage'))

    if _matches_api_prefix('/usage'):
        return filebrowser_usage(_resource_suffix_from_path('/usage'))

    return Response('Not Found', status=404, mimetype='text/plain; charset=utf-8')


@app.route('/api/users', methods=['GET'])
def filebrowser_list_users():
    user_row, error = require_api_auth()
    if error:
        return error
    return fb_json([filebrowser_user_from_row(user_row)])


@app.route('/api/users/<int:user_id>', methods=['GET', 'PUT', 'DELETE'])
def filebrowser_update_user(user_id):
    user_row, error = require_api_auth()
    if error:
        return error
    if user_row[0] != user_id:
        return fb_error('Forbidden', 403)

    if request.method == 'GET':
        return fb_json(filebrowser_user_from_row(user_row))

    if request.method == 'DELETE':
        return fb_error('Deleting users is unsupported', 403)

    payload = request.get_json(silent=True) or {}
    data = payload.get('data') or {}
    which = payload.get('which') or []
    current_password = payload.get('current_password') or ''

    if 'password' in which and data.get('password'):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE id = ?", (user_id,))
        current_row = cursor.fetchone()
        if not current_row or not check_password_hash(current_row[0], current_password):
            conn.close()
            return fb_error('Wrong credentials', 403)
        cursor.execute("UPDATE users SET password = ? WHERE id = ?", (generate_password_hash(data['password']), user_id))
        conn.commit()
        conn.close()

    prefs = get_user_preferences(user_id)
    for key in ('viewMode', 'sorting', 'locale', 'dateFormat', 'singleClick', 'hideDotfiles', 'redirectAfterCopyMove', 'aceEditorTheme'):
        if key in data:
            prefs[key] = data[key]
    USER_PREFERENCES[user_id] = prefs
    return Response('', status=200)


@app.route('/api/search/', methods=['GET'])
@app.route('/api/search/<path:base_path>', methods=['GET'])
def filebrowser_search(base_path=''):
    user_row, error = require_api_auth()
    if error:
        return error
    query = (request.args.get('query') or '').strip().lower()
    if not query:
        return Response('', status=200, mimetype='application/x-ndjson')

    base_folder_id = resolve_folder_path_for_user(base_path, user_row[0])
    if base_path and base_folder_id is None:
        return fb_error('Not found', 404)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    results = []

    def walk(folder_id):
        cursor.execute("SELECT id, name FROM folders WHERE parent_id IS ? AND owner_id = ?", (folder_id, user_row[0]))
        folder_rows = cursor.fetchall()
        current_parent = folder_path_from_id(folder_id, user_row[0])
        for row in folder_rows:
            item = serialize_fb_item(row, current_parent, True)
            if query in row[1].lower():
                results.append(item)
            walk(row[0])

        cursor.execute("SELECT id, file_name, file_size, upload_date FROM files WHERE folder_id IS ? AND owner_id = ?", (folder_id, user_row[0]))
        for row in cursor.fetchall():
            item = serialize_fb_item(row, current_parent, False)
            if query in row[1].lower():
                results.append(item)

    walk(base_folder_id)
    conn.close()
    return Response('\n'.join(json.dumps(item, ensure_ascii=False) for item in results), status=200, mimetype='application/x-ndjson')


@app.route('/api/folders', methods=['POST'])
@login_required
def api_create_folder():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get('name') or '').strip()
    if not name:
        return api_error('Tên thư mục không được để trống')

    parent_id = payload.get('parent_id')
    if parent_id in ('None', '', None, 'root'):
        parent_id = None

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO folders (name, parent_id, owner_id) VALUES (?, ?, ?)", (name, parent_id, current_user.id))
    folder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return api_success({'folder': {'id': folder_id, 'name': name, 'parent_id': parent_id}}, 201)


@app.route('/api/folders/<int:folder_id>', methods=['PATCH'])
@login_required
def api_rename_folder(folder_id):
    payload = request.get_json(silent=True) or request.form
    new_name = (payload.get('name') or '').strip()
    if not new_name:
        return api_error('Tên thư mục không được để trống')

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE folders SET name = ? WHERE id = ? AND owner_id = ?", (new_name, folder_id, current_user.id))
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    if not changed:
        return api_error('Không tìm thấy thư mục', 404)
    return api_success({'folder': {'id': folder_id, 'name': new_name}})


@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
@login_required
def api_delete_folder(folder_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM folders WHERE id = ? AND owner_id = ?", (folder_id, current_user.id))
    if not cursor.fetchone():
        conn.close()
        return api_error('Không có quyền hoặc không tìm thấy thư mục', 404)
    cursor.execute("DELETE FROM folders WHERE id = ? OR parent_id = ?", (folder_id, folder_id))
    cursor.execute("UPDATE files SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
    conn.commit()
    conn.close()
    return api_success({'message': 'Đã xóa thư mục, file được chuyển về root'})


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_upload_file():
    if 'file' not in request.files:
        return api_error('Không có file được gửi lên')
    file = request.files['file']
    if not file or file.filename == '':
        return api_error('Tên file không hợp lệ')
    folder_id = request.form.get('folder_id') or request.args.get('folder_id')
    if folder_id in ('None', '', None, 'root'):
        folder_id = None
    file_id, job_id = queue_upload(file, folder_id)
    return api_success({'file_id': file_id, 'job_id': job_id}, 202)


@app.route('/api/files/<int:file_id>/move', methods=['POST'])
@login_required
def api_move_file(file_id):
    payload = request.get_json(silent=True) or request.form
    target_folder_id = payload.get('target_folder_id')
    if target_folder_id in ('root', '', None, 'None'):
        target_folder_id = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE files SET folder_id = ? WHERE id = ? AND owner_id = ?",
                   (target_folder_id, file_id, current_user.id))
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    if not changed:
        return api_error('Không tìm thấy file', 404)
    return api_success({'message': 'Đã chuyển file'})


@app.route('/api/files/<int:file_id>/public-link', methods=['POST'])
@login_required
def api_toggle_public_link(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT owner_id, public_token FROM files WHERE id = ?", (file_id,))
    result = cursor.fetchone()
    if not result or result[0] != current_user.id:
        conn.close()
        return api_error('Không có quyền với file này', 404)

    public_url = None
    if result[1]:
        cursor.execute("UPDATE files SET public_token = NULL WHERE id = ?", (file_id,))
    else:
        new_token = str(uuid.uuid4())
        cursor.execute("UPDATE files SET public_token = ? WHERE id = ?", (new_token, file_id))
        public_url = url_for('public_download', token=new_token, _external=True)

    conn.commit()
    conn.close()
    return api_success({'public_url': public_url})


@app.route('/api/files/<int:file_id>/retry', methods=['POST'])
@login_required
def api_retry_file(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT file_name, job_id, message_ids FROM files WHERE id = ? AND owner_id = ?",
                   (file_id, current_user.id))
    result = cursor.fetchone()
    conn.close()
    if not result:
        return api_error('Không tìm thấy file', 404)

    file_name, job_id, message_ids = result
    original_path = os.path.join(TEMP_UPLOAD, job_id, file_name) if job_id else None
    if not original_path or not os.path.exists(original_path):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET status = 'Error', error_message = ? WHERE id = ?",
                       ('Original file not found. Please re-upload.', file_id))
        conn.commit()
        conn.close()
        return api_error('Không tìm thấy file gốc, hãy tải lên lại', 409)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE files SET status = 'Processing', error_message = NULL WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()

    thread = threading.Thread(target=process_file_background,
                              args=(file_id, original_path, None, current_user.id, job_id))
    thread.start()
    return api_success({'message': 'Đang thử tải lại'})


@app.route('/api/files/<int:file_id>/stop', methods=['POST'])
@login_required
def api_stop_upload(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM files WHERE id = ? AND owner_id = ?", (file_id, current_user.id))
    if not cursor.fetchone():
        conn.close()
        return api_error('Không tìm thấy file', 404)
    cursor.execute("UPDATE files SET status = 'Stopped', error_message = NULL WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()
    return api_success({'message': 'Đã yêu cầu dừng upload'})


@app.route('/api/files/<int:file_id>', methods=['DELETE'])
@login_required
def api_delete_file(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT owner_id, message_ids FROM files WHERE id=?", (file_id,))
    result = cursor.fetchone()
    if not result or result[0] != current_user.id:
        conn.close()
        return api_error('Không có quyền với file này', 404)
    if result[1]:
        for msg_id in result[1].split(', '):
            try:
                if msg_id and msg_id != "EMPTY":
                    url = f"http://bot-api:8081/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
                    http_requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'message_id': msg_id}, timeout=30)
            except Exception as e:
                log_message(f"Failed to delete message {msg_id}: {e}")
    cursor.execute("DELETE FROM files WHERE id=?", (file_id,))
    conn.commit()
    conn.close()
    return api_success({'message': 'Đã xóa file'})


@app.route('/api/files/<int:file_id>/download-url', methods=['GET'])
@login_required
def api_download_url(file_id):
    return api_success({'url': url_for('download_and_decrypt', file_id=file_id)})


@app.route('/assets/<path:filename>')
def frontend_assets(filename):
    assets_dir = os.path.join(FRONTEND_DIST, 'assets')
    if os.path.exists(os.path.join(assets_dir, filename)):
        return send_from_directory(assets_dir, filename)
    return "Not Found", 404


@app.route('/img/<path:filename>')
def frontend_public_images(filename):
    img_dir = os.path.join(FRONTEND_DIST, 'img')
    if os.path.exists(os.path.join(img_dir, filename)):
        return send_from_directory(img_dir, filename)
    return "Not Found", 404


# --- Main File Browser ---
@app.route('/', methods=['GET', 'POST'])
@app.route('/folder/<int:folder_id>', methods=['GET', 'POST'])
@login_required
def index(folder_id=None):
    if request.method == 'POST':
        if 'file' not in request.files:
            return redirect(request.url)
        file = request.files['file']
        if file and file.filename != '':
            queue_upload(file, folder_id)
            return redirect(url_for('index', folder_id=folder_id) if folder_id else url_for('index'))

    files_info, folders_info, current_folder_name, parent_folder_id = fetch_directory_contents(folder_id)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM folders WHERE owner_id = ?", (current_user.id,))
    all_user_folders = [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]
    conn.close()

    if frontend_ready():
        return send_from_directory(FRONTEND_DIST, 'index.html')

    return render_template('index.html', files_info=files_info, folders_info=folders_info,
                           current_folder_id=folder_id, current_folder_name=current_folder_name,
                           parent_folder_id=parent_folder_id, all_user_folders=all_user_folders)


def fetch_directory_contents(folder_id):
    init_db()
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT id, name FROM folders WHERE parent_id IS ? AND owner_id = ?", (folder_id, current_user.id))
    folders_info = [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]

    cursor.execute("SELECT id, file_name, file_size, chunk_list, status, error_message, job_id, public_token FROM files WHERE folder_id IS ? AND owner_id = ?",
                   (folder_id, current_user.id))
    results = cursor.fetchall()

    files_info = [serialize_file(result) for result in results]

    current_folder_name = "Home"
    parent_folder_id = None
    if folder_id:
        cursor.execute("SELECT name, parent_id FROM folders WHERE id = ?", (folder_id,))
        folder_data = cursor.fetchone()
        if folder_data:
            current_folder_name, parent_folder_id = folder_data

    conn.close()
    return files_info, folders_info, current_folder_name, parent_folder_id


# --- Folder Routes ---
@app.route('/create_folder', methods=['POST'])
@login_required
def create_folder():
    name = request.form.get('name')
    parent_id = request.form.get('parent_id')
    if parent_id == 'None' or not parent_id:
        parent_id = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO folders (name, parent_id, owner_id) VALUES (?, ?, ?)", (name, parent_id, current_user.id))
    conn.commit()
    conn.close()
    flash('Folder created successfully', 'success')
    return redirect(url_for('index', folder_id=parent_id) if parent_id else url_for('index'))


@app.route('/rename_folder', methods=['POST'])
@login_required
def rename_folder():
    folder_id = request.form.get('folder_id')
    new_name = request.form.get('name')
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE folders SET name = ? WHERE id = ? AND owner_id = ?", (new_name, folder_id, current_user.id))
    conn.commit()
    conn.close()
    flash('Folder renamed', 'success')
    return redirect(request.referrer)


@app.route('/delete_folder/<int:folder_id>')
@login_required
def delete_folder(folder_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM folders WHERE id = ? AND owner_id = ?", (folder_id, current_user.id))
    if not cursor.fetchone():
        conn.close()
        return "Unauthorized", 403
    cursor.execute("DELETE FROM folders WHERE id = ? OR parent_id = ?", (folder_id, folder_id))
    cursor.execute("UPDATE files SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
    conn.commit()
    conn.close()
    flash('Folder deleted. Files moved to root.', 'success')
    return redirect(url_for('index'))


@app.route('/move_file', methods=['POST'])
@login_required
def move_file():
    file_id = request.form.get('file_id')
    target_folder_id = request.form.get('target_folder_id')
    if target_folder_id == 'root':
        target_folder_id = None
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE files SET folder_id = ? WHERE id = ? AND owner_id = ?",
                   (target_folder_id, file_id, current_user.id))
    conn.commit()
    conn.close()
    flash('File moved successfully', 'success')
    return redirect(request.referrer)


# --- Sharing Routes ---
@app.route('/toggle_public_link/<int:file_id>', methods=['POST'])
@login_required
def toggle_public_link(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT owner_id, public_token FROM files WHERE id = ?", (file_id,))
    result = cursor.fetchone()
    if not result or result[0] != current_user.id:
        conn.close()
        return "Unauthorized", 403
    
    if result[1]: # Revoke
        cursor.execute("UPDATE files SET public_token = NULL WHERE id = ?", (file_id,))
        flash("Public link revoked.", "success")
    else: # Generate
        new_token = str(uuid.uuid4())
        cursor.execute("UPDATE files SET public_token = ? WHERE id = ?", (new_token, file_id))
        flash("Public link generated.", "success")
        
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for('index'))


# --- Admin Routes ---
@app.route('/admin')
@login_required
def admin_panel():
    if current_user.role != 'admin':
        return "Access Forbidden", 403
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role FROM users")
    users = [{"id": r[0], "username": r[1], "role": r[2]} for r in cursor.fetchall()]
    conn.close()
    return render_template('admin.html', users=users)


@app.route('/admin/create_user', methods=['POST'])
@login_required
def create_user():
    if current_user.role != 'admin':
        return "Access Forbidden", 403
    username = request.form.get('username')
    password = request.form.get('password')
    role = request.form.get('role', 'user')
    hashed_pw = generate_password_hash(password)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, hashed_pw, role))
        conn.commit()
        flash('User created successfully', 'success')
    except sqlite3.IntegrityError:
        flash('Username already exists', 'error')
    conn.close()
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_user/<int:user_id>')
@login_required
def delete_user(user_id):
    if current_user.role != 'admin' or current_user.id == user_id:
        return "Access Forbidden", 403
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash('User deleted successfully', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/edit_user', methods=['POST'])
@login_required
def edit_user():
    if current_user.role != 'admin':
        return "Access Forbidden", 403
    user_id = request.form.get('user_id')
    new_role = request.form.get('role')
    new_password = request.form.get('password')
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    if new_role:
        cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    if new_password:
        hashed_pw = generate_password_hash(new_password)
        cursor.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, user_id))
    conn.commit()
    conn.close()
    flash('User updated successfully', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/backup')
@login_required
def backup_db():
    if current_user.role != 'admin':
        return "Access Forbidden", 403
    return send_file(DATABASE_FILE, as_attachment=True,
                     download_name=f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')


@app.route('/admin/restore', methods=['POST'])
@login_required
def restore_db():
    if current_user.role != 'admin':
        return "Access Forbidden", 403
    if 'db_file' not in request.files:
        flash('No file part', 'error')
        return redirect(url_for('admin_panel'))
    file = request.files['db_file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(url_for('admin_panel'))
    if file and file.filename.endswith('.db'):
        file.save(DATABASE_FILE)
        flash('Database restored successfully! Please restart the app.', 'success')
    else:
        flash('Invalid file format', 'error')
    return redirect(url_for('admin_panel'))


# --- Upload Processing ---
@app.route('/stop_upload/<int:file_id>')
@login_required
def stop_upload(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM files WHERE id = ? AND owner_id = ?", (file_id, current_user.id))
    if cursor.fetchone():
        cursor.execute("UPDATE files SET status = 'Stopped' WHERE id = ?", (file_id,))
        conn.commit()
        flash('Upload stopping...', 'info')
    else:
        flash('Unauthorized', 'error')
    conn.close()
    return redirect(url_for('index'))


@app.route('/retry/<int:file_id>')
@login_required
def retry_file(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT file_name, job_id, message_ids FROM files WHERE id = ? AND owner_id = ?",
                   (file_id, current_user.id))
    result = cursor.fetchone()
    if not result:
        conn.close()
        flash('File not found or unauthorized', 'error')
        return redirect(url_for('index'))

    file_name, job_id, message_ids = result
    cursor.execute("UPDATE files SET status = 'Processing' WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()

    job_dir = os.path.join(TEMP_UPLOAD, job_id) if job_id else None
    file_path = os.path.join(job_dir, file_name) if job_dir else None

    if file_id and file_path and os.path.exists(file_path):
        thread = threading.Thread(target=process_file_background,
                                  args=(file_id, file_path, None, current_user.id, job_id))
        thread.start()
        flash('Resuming upload...', 'success')
    else:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET status = 'Error' WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        flash('Original file not found. Please re-upload.', 'error')

    return redirect(url_for('index'))


# --- Encryption ---
def split_and_encrypt(input_file, output_directory, key, file_id=None):
    chunk_size = 1900 * 1024 * 1024  # 1.9GB
    num_chunks = 0
    os.makedirs(output_directory, exist_ok=True)

    with open(input_file, 'rb') as f:
        while True:
            if is_stopped(file_id):
                raise Exception("Encryption stopped by user")
            chunk = f.read(chunk_size)
            if not chunk:
                break
            num_chunks += 1
            cipher = AES.new(key, AES.MODE_EAX)
            ciphertext, tag = cipher.encrypt_and_digest(chunk)
            nonce = cipher.nonce
            original_filename = os.path.basename(input_file)
            chunk_filename = os.path.join(output_directory, f'{original_filename}.chunk_{num_chunks}.enc')
            with open(chunk_filename, 'wb') as chunk_file:
                chunk_file.write(nonce)
                chunk_file.write(tag)
                chunk_file.write(ciphertext)

    print(f'Split and encrypted {input_file} into {num_chunks} chunks.')


def decrypt_and_reassemble(chunk_filenames, output_file, key_hex):
    key = bytes.fromhex(key_hex)
    output_file_path = os.path.join(TEMP_DOWNLOAD, output_file)
    os.makedirs(TEMP_DOWNLOAD, exist_ok=True)

    with open(output_file_path, 'wb') as output_f:
        for chunk_filename in chunk_filenames:
            with open(chunk_filename, 'rb') as chunk_file:
                nonce = chunk_file.read(16)
                tag = chunk_file.read(16)
                ciphertext = chunk_file.read()
            cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
            decrypted_chunk = cipher.decrypt_and_verify(ciphertext, tag)
            output_f.write(decrypted_chunk)

    print(f'Decrypted and reassembled into {output_file_path}.')


# --- Telegram Upload/Download ---
def send_file_to_telegram(file_content, filename):
    url = f"http://bot-api:8081/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {'document': (filename, file_content)}
    data = {'chat_id': TELEGRAM_CHAT_ID}
    response = http_requests.post(url, files=files, data=data, timeout=120)
    return response


def upload_chunk(chunk_path, file_id=None, max_retries=5):
    retry_count = 0
    filename = os.path.basename(chunk_path)
    while retry_count < max_retries:
        if is_stopped(file_id):
            return None
        try:
            with open(chunk_path, 'rb') as file:
                response = send_file_to_telegram(file.read(), filename)
                if response.status_code == 200:
                    data = response.json()
                    telegram_file_id = data['result']['document']['file_id']
                    telegram_message_id = str(data['result']['message_id'])
                    return telegram_file_id, telegram_message_id
                else:
                    raise Exception(f"Upload failed: {response.status_code} - {response.text}")
        except Exception as e:
            if is_stopped(file_id):
                return None
            log_message(f"Error uploading {filename}: {e}, retrying...")
            retry_count += 1
            time.sleep(1)
    return None


def upload_to_telegram(output_directory, file_id=None):
    log_message("Uploading chunks to Telegram...")
    filenames = os.listdir(output_directory)
    chunk_files = [f for f in filenames if f.endswith('.enc')]
    sorted_filenames = sorted(chunk_files, key=numerical_sort_key)
    chunks_paths = [os.path.join(output_directory, fn) for fn in sorted_filenames]

    total_chunks = len(chunks_paths)
    chunk_results = [None] * total_chunks

    # Resume logic
    if file_id:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chunk_list, message_ids FROM files WHERE id = ?", (file_id,))
        db_res = cursor.fetchone()
        conn.close()
        if db_res:
            old_urls = db_res[0].split(', ') if db_res[0] else []
            old_ids = db_res[1].split(', ') if db_res[1] else []
            if len(old_urls) == total_chunks:
                for i in range(total_chunks):
                    if old_urls[i] != "EMPTY":
                        chunk_results[i] = (old_urls[i], old_ids[i])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future_to_index = {}
        for i, path in enumerate(chunks_paths):
            if chunk_results[i] is None:
                future_to_index[executor.submit(upload_chunk, path, file_id=file_id)] = i

        stop_signal_detected = False
        for future in concurrent.futures.as_completed(future_to_index):
            if not stop_signal_detected and is_stopped(file_id):
                log_message(f"Stop signal detected for file {file_id}.")
                stop_signal_detected = True
                for f in future_to_index:
                    f.cancel()

            index = future_to_index[future]
            try:
                result = future.result()
                if result:
                    chunk_results[index] = result
                    if file_id:
                        update_progressive_status(file_id, chunk_results)
            except Exception as exc:
                if not stop_signal_detected:
                    log_message(f'Chunk {index + 1} error: {exc}')

        if stop_signal_detected:
            return None, None

    if None in chunk_results:
        return None, None

    chunks_urls = [res[0] for res in chunk_results]  # These are telegram_file_id
    message_ids = [res[1] for res in chunk_results]  # These are telegram_message_id
    return chunks_urls, message_ids


def update_progressive_status(file_id, chunk_results):
    try:
        chunks_urls = []
        message_ids = []
        for res in chunk_results:
            if res is not None:
                chunks_urls.append(res[0])
                message_ids.append(res[1])
            else:
                chunks_urls.append("EMPTY")
                message_ids.append("EMPTY")
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET chunk_list = ?, message_ids = ? WHERE id = ?",
                       (', '.join(chunks_urls), ', '.join(message_ids), file_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log_message(f"Failed to update status for file {file_id}: {e}")


def process_file_background(file_id, file_path, folder_id, user_id, job_id=None):
    try:
        log_message(f"Starting background processing for file ID {file_id}")
        if not job_id:
            job_id = str(uuid.uuid4())

        job_dir = os.path.join(TEMP_UPLOAD, job_id)
        chunks_dir = os.path.join(TEMP_CHUNKS, job_id)
        os.makedirs(job_dir, exist_ok=True)
        os.makedirs(chunks_dir, exist_ok=True)

        file_name = os.path.basename(file_path)
        new_file_path = os.path.join(job_dir, file_name)

        if os.path.exists(file_path) and os.path.abspath(file_path) != os.path.abspath(new_file_path):
            shutil.move(file_path, new_file_path)

        # Get or create encryption key
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT key_hex FROM files WHERE id = ?", (file_id,))
        existing_key = cursor.fetchone()[0]
        conn.close()

        if existing_key and len(existing_key) > 0:
            key = bytes.fromhex(existing_key)
            key_hex = existing_key
        else:
            key = get_random_bytes(16)
            key_hex = key.hex()
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE files SET key_hex = ?, error_message = NULL WHERE id = ?", (key_hex, file_id))
            conn.commit()
            conn.close()

        split_and_encrypt(new_file_path, chunks_dir, key, file_id=file_id)
        chunks_urls, message_ids = upload_to_telegram(chunks_dir, file_id=file_id)

        if not chunks_urls:
            raise Exception("Failed to upload all chunks to Telegram.")

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET status = 'Ready', error_message = NULL WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()

        shutil.rmtree(job_dir, ignore_errors=True)
        shutil.rmtree(chunks_dir, ignore_errors=True)
        log_message(f"Background processing completed for file ID {file_id}")

    except Exception as e:
        if is_stopped(file_id):
            log_message(f"File {file_id} stopped.")
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE files SET status = 'Stopped', error_message = NULL WHERE id = ?", (file_id,))
            conn.commit()
            conn.close()
            return
        error_text = str(e)[:1000]
        log_message(f"Error processing file {file_id}: {error_text}")
        log_message(traceback.format_exc())
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE files SET status = 'Error', error_message = ? WHERE id = ?", (error_text, file_id))
            conn.commit()
            conn.close()
        except:
            pass


# --- Download ---


def fetch_telegram_url(telegram_file_id, max_retries=3):
    if not telegram_file_id or telegram_file_id == "EMPTY":
        return None
    url = f"http://bot-api:8081/bot{TELEGRAM_BOT_TOKEN}/getFile"
    retry_count = 0
    while retry_count < max_retries:
        try:
            response = http_requests.get(url, params={'file_id': telegram_file_id})
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    file_path = data['result']['file_path']
                    return file_path  # Returns absolute local path like /var/lib/telegram-bot-api/...
                return None
            elif response.status_code == 429:
                retry_after = response.json().get('parameters', {}).get('retry_after', 1)
                log_message(f"Rate limited. Retrying after {retry_after}s...")
                time.sleep(retry_after)
                retry_count += 1
                continue
            else:
                retry_count += 1
                time.sleep(1)
        except Exception as e:
            log_message(f"Error fetching URL for {telegram_file_id}: {e}")
            retry_count += 1
            time.sleep(1)
    return None


def get_fresh_telegram_urls(telegram_file_ids):
    total = len(telegram_file_ids)
    refreshed_urls = [None] * total
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_index = {executor.submit(fetch_telegram_url, f_id): i for i, f_id in enumerate(telegram_file_ids)}
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                url = future.result()
                if url:
                    refreshed_urls[index] = url
            except Exception as e:
                log_message(f"Error refreshing index {index}: {e}")
    return refreshed_urls


def prepare_decrypted_file(file_name, chunks_urls, key_hex):
    os.makedirs(TEMP_CHUNKS, exist_ok=True)
    os.makedirs(TEMP_DOWNLOAD, exist_ok=True)

    if chunks_urls and len(chunks_urls) > 0:
        log_message(f"Fetching {len(chunks_urls)} fresh Telegram paths for {file_name}...")
        refreshed_urls = get_fresh_telegram_urls(chunks_urls)
        if refreshed_urls and None not in refreshed_urls:
            local_chunk_paths = refreshed_urls
        else:
            log_message("Failed to resolve dynamic telegram links.")
            raise FileNotFoundError("Failed to resolve file paths from Telegram API")
    else:
        raise FileNotFoundError("No Telegram chunks available")

    decrypt_and_reassemble(local_chunk_paths, file_name, key_hex)
    return os.path.join(os.getcwd(), TEMP_DOWNLOAD, file_name)


def read_decrypted_content(file_name, chunks_urls, key_hex):
    decrypted_file_path = prepare_decrypted_file(file_name, chunks_urls, key_hex)
    try:
        with open(decrypted_file_path, 'rb') as source:
            return source.read()
    finally:
        shutil.rmtree(TEMP_DOWNLOAD, ignore_errors=True)
        os.makedirs(TEMP_DOWNLOAD, exist_ok=True)


def process_download(file_name, chunks_urls, message_ids, key_hex, file_id=None, as_attachment=True):
    try:
        decrypted_file_path = prepare_decrypted_file(file_name, chunks_urls, key_hex)

        @after_this_request
        def cleanup(response):
            shutil.rmtree(TEMP_DOWNLOAD, ignore_errors=True)
            os.makedirs(TEMP_DOWNLOAD, exist_ok=True)
            return response

        return send_file(
            decrypted_file_path,
            as_attachment=as_attachment,
            download_name=file_name,
        )

    except Exception as e:
        log_message(f"Decryption error: {e}")
        shutil.rmtree(TEMP_DOWNLOAD, ignore_errors=True)
        os.makedirs(TEMP_DOWNLOAD, exist_ok=True)
        return "Decryption failed", 500


@app.route('/download/<int:file_id>', methods=['GET'])
@login_required
def download_and_decrypt(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""SELECT file_name, chunk_list, message_ids, key_hex, owner_id
                      FROM files WHERE id = ? AND owner_id = ?""",
                   (file_id, current_user.id))
    result = cursor.fetchone()
    conn.close()

    if not result:
        return "File not found or unauthorized", 404

    file_name, chunk_list, message_ids_str, key_hex, owner_id = result
    chunks_urls = chunk_list.split(', ') if chunk_list else []
    message_ids = message_ids_str.split(', ') if message_ids_str else []
    inline = (request.args.get('inline') or 'false').lower() == 'true'

    return process_download(file_name, chunks_urls, message_ids, key_hex, file_id=file_id, as_attachment=not inline)


@app.route('/s/<token>', methods=['GET'])
def public_download(token):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""SELECT id, file_name, file_size, upload_date
                      FROM files WHERE public_token = ?""", (token,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        return "Invalid or expired link", 404

    file_id, file_name, file_size, upload_date = result
    formatted_size = convert_bytes(file_size)
    
    file_info = {
        'id': file_id,
        'file_name': file_name,
        'formatted_size': formatted_size,
        'upload_date': upload_date,
        'token': token
    }

    return render_template('public_download.html', file_info=file_info)


@app.route('/s/<token>/download', methods=['GET'])
def execute_public_download(token):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("""SELECT id, file_name, chunk_list, message_ids, key_hex
                      FROM files WHERE public_token = ?""", (token,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        return "Invalid or expired link", 404

    file_id, file_name, chunk_list, message_ids_str, key_hex = result
    chunks_urls = chunk_list.split(', ') if chunk_list else []
    message_ids = message_ids_str.split(', ') if message_ids_str else []

    return process_download(file_name, chunks_urls, message_ids, key_hex, file_id=file_id)


# --- Delete ---
@app.route('/delete/<int:file_id>', methods=['GET'])
@login_required
def delete_file_entry(file_id):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT owner_id, message_ids FROM files WHERE id=?", (file_id,))
    result = cursor.fetchone()
    if not result or result[0] != current_user.id:
        conn.close()
        return "Unauthorized", 403
    if result and result[1]:
        message_ids = result[1].split(', ')
        for msg_id in message_ids:
            try:
                if msg_id and msg_id != "EMPTY":
                    url = f"http://bot-api:8081/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
                    http_requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'message_id': msg_id})
            except Exception as e:
                log_message(f"Failed to delete message {msg_id}: {e}")
    cursor.execute("DELETE FROM files WHERE id=?", (file_id,))
    conn.commit()
    conn.close()
    flash('Deleted file from Database and Telegram', 'success')
    return redirect(url_for('index'))


# --- Export / Import ---
@app.route('/export', methods=['POST'])
def export_files():
    selected_ids = request.form.getlist('selected_ids[]')
    export_dir = os.path.join(DATA_DIR, 'temp_export')
    export_db_path = os.path.join(export_dir, 'exported_files.db')
    os.makedirs(export_dir, exist_ok=True)
    if os.path.exists(export_db_path):
        os.remove(export_db_path)

    conn = sqlite3.connect(export_db_path)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS files
                      (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      file_name TEXT, chunk_list TEXT, message_ids TEXT,
                      key_hex TEXT, file_size INTEGER, upload_date TEXT)''')

    main_conn = sqlite3.connect(DATABASE_FILE)
    main_cursor = main_conn.cursor()
    for file_id in selected_ids:
        main_cursor.execute("SELECT file_name, chunk_list, message_ids, key_hex, file_size, upload_date FROM files WHERE id=?",
                            (file_id,))
        file_info = main_cursor.fetchone()
        if file_info:
            cursor.execute("INSERT INTO files (file_name, chunk_list, message_ids, key_hex, file_size, upload_date) VALUES (?, ?, ?, ?, ?, ?)",
                           file_info)
    main_conn.close()
    conn.commit()
    conn.close()
    flash('Files exported successfully!', 'success')
    return send_file(export_db_path, as_attachment=True, download_name='exported_files.db', mimetype='application/octet-stream')


@app.route('/import', methods=['POST'])
def import_db():
    import_dir = os.path.join(DATA_DIR, 'temp_import')
    if os.path.exists(import_dir):
        shutil.rmtree(import_dir, ignore_errors=True)
    if 'db_file' not in request.files:
        flash('No file part', 'error')
        return redirect(request.url)
    file = request.files['db_file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(request.url)
    if file and file.filename.endswith('.db'):
        os.makedirs(import_dir, exist_ok=True)
        temp_path = os.path.join(import_dir, file.filename)
        file.save(temp_path)
        try:
            validate_and_merge_db(temp_path)
            flash('Database imported successfully!', 'success')
        except Exception as e:
            flash(str(e), 'error')
            return redirect(url_for('index'))
        return redirect(url_for('index'))
    else:
        flash('Invalid file format', 'error')
        return redirect(url_for('index'))


def validate_and_merge_db(import_path):
    """Validate and merge an imported database into the main database."""
    import_conn = sqlite3.connect(import_path)
    import_cursor = import_conn.cursor()
    import_cursor.execute("SELECT file_name, chunk_list, message_ids, key_hex, file_size, upload_date FROM files")
    rows = import_cursor.fetchall()
    import_conn.close()

    if not rows:
        raise Exception("No files found in imported database")

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    for row in rows:
        cursor.execute("""INSERT INTO files (file_name, chunk_list, message_ids, key_hex, file_size, upload_date, owner_id)
                          VALUES (?, ?, ?, ?, ?, ?, ?)""", (*row, current_user.id))
    conn.commit()
    conn.close()


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5010)
