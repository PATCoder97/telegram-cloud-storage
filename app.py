from flask import Flask, render_template, request, send_file, redirect, url_for, after_this_request, flash, Response
from Cryptodome.Cipher import AES
from Cryptodome.Random import get_random_bytes
import os
import sqlite3
from datetime import datetime
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

app = Flask(__name__)
app.secret_key = os.urandom(16)

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
                      job_id TEXT,
                      public_token TEXT,
                      FOREIGN KEY(folder_id) REFERENCES folders(id),
                      FOREIGN KEY(owner_id) REFERENCES users(id))''')

    # Schema migration
    cursor.execute("PRAGMA table_info(files)")
    columns = [col[1] for col in cursor.fetchall()]
    for col, default in [('message_ids', 'TEXT'), ('folder_id', 'INTEGER'), ('owner_id', 'INTEGER'),
                         ('status', "TEXT DEFAULT 'Ready'"), ('job_id', 'TEXT'), ('public_token', 'TEXT')]:
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
            return redirect(url_for('index'))
        flash('Invalid username or password', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


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
            os.makedirs(TEMP_UPLOAD, exist_ok=True)
            file_path = os.path.join(TEMP_UPLOAD, file.filename)
            file.save(file_path)

            file_size = os.path.getsize(file_path)
            upload_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            job_id = str(uuid.uuid4())

            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("""INSERT INTO files
                              (file_name, chunk_list, message_ids, key_hex, file_size, upload_date, folder_id, owner_id, status, job_id)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (file.filename, '', '', '', file_size, upload_date, folder_id, current_user.id, 'Processing', job_id))
            file_id = cursor.lastrowid
            conn.commit()
            conn.close()

            thread = threading.Thread(target=process_file_background,
                                      args=(file_id, file_path, folder_id, current_user.id, job_id))
            thread.start()
            return redirect(url_for('index', folder_id=folder_id) if folder_id else url_for('index'))

    files_info, folders_info, current_folder_name, parent_folder_id = fetch_directory_contents(folder_id)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM folders WHERE owner_id = ?", (current_user.id,))
    all_user_folders = [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]
    conn.close()

    return render_template('index.html', files_info=files_info, folders_info=folders_info,
                           current_folder_id=folder_id, current_folder_name=current_folder_name,
                           parent_folder_id=parent_folder_id, all_user_folders=all_user_folders)


def fetch_directory_contents(folder_id):
    init_db()
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT id, name FROM folders WHERE parent_id IS ? AND owner_id = ?", (folder_id, current_user.id))
    folders_info = [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]

    cursor.execute("SELECT id, file_name, file_size, chunk_list, status, job_id, public_token FROM files WHERE folder_id IS ? AND owner_id = ?",
                   (folder_id, current_user.id))
    results = cursor.fetchall()

    files_info = []
    for result in results:
        f_id, f_name, f_size, f_list, f_status, f_job_id, pub_token = result
        chunk_amount = len(f_list.split(', ')) if f_list else 0
        formatted_size = convert_bytes(f_size)
        files_info.append({
            'id': f_id, 'file_name': f_name, 'formatted_size': formatted_size,
            'chunk_amount': chunk_amount, 'status': f_status, 'job_id': f_job_id,
            'public_token': pub_token
        })

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
    response = http_requests.post(url, files=files, data=data)
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
            print(f"Error uploading: {e}, retrying...")
            retry_count += 1
            time.sleep(1)
    return None


def upload_to_telegram(output_directory, file_id=None):
    print(f"Uploading chunks to Telegram...")
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
                print(f"Stop signal detected for file {file_id}.")
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
                    print(f'Chunk {index + 1} error: {exc}')

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
        print(f"Failed to update status for file {file_id}: {e}")


def process_file_background(file_id, file_path, folder_id, user_id, job_id=None):
    try:
        print(f"Starting background processing for file ID {file_id}")
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
            cursor.execute("UPDATE files SET key_hex = ? WHERE id = ?", (key_hex, file_id))
            conn.commit()
            conn.close()

        split_and_encrypt(new_file_path, chunks_dir, key, file_id=file_id)
        chunks_urls, message_ids = upload_to_telegram(chunks_dir, file_id=file_id)

        if not chunks_urls:
            raise Exception("Failed to upload all chunks to Telegram.")

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET status = 'Ready' WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()

        shutil.rmtree(job_dir, ignore_errors=True)
        shutil.rmtree(chunks_dir, ignore_errors=True)
        print(f"Background processing completed for file ID {file_id}")

    except Exception as e:
        if is_stopped(file_id):
            print(f"File {file_id} stopped.")
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE files SET status = 'Stopped' WHERE id = ?", (file_id,))
            conn.commit()
            conn.close()
            return
        print(f"Error processing file {file_id}: {e}")
        try:
            conn = sqlite3.connect(DATABASE_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE files SET status = 'Error' WHERE id = ?", (file_id,))
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
                print(f"Rate limited. Retrying after {retry_after}s...")
                time.sleep(retry_after)
                retry_count += 1
                continue
            else:
                retry_count += 1
                time.sleep(1)
        except Exception as e:
            print(f"Error fetching URL for {telegram_file_id}: {e}")
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
                print(f"Error refreshing index {index}: {e}")
    return refreshed_urls


def process_download(file_name, chunks_urls, message_ids, key_hex, file_id=None):
    os.makedirs(TEMP_CHUNKS, exist_ok=True)
    os.makedirs(TEMP_DOWNLOAD, exist_ok=True)

    if chunks_urls and len(chunks_urls) > 0:
        print(f"Fetching {len(chunks_urls)} fresh Telegram paths for {file_name}...")
        refreshed_urls = get_fresh_telegram_urls(chunks_urls)
        if refreshed_urls and None not in refreshed_urls:
            local_chunk_paths = refreshed_urls
        else:
            print("Failed to resolve dynamic telegram links.")
            return "Failed to resolve file paths from Telegram API", 404

    try:
        # local_chunk_paths contains absolute file paths mounted natively through docker volumes
        decrypt_and_reassemble(local_chunk_paths, file_name, key_hex)
        decrypted_file_path = os.path.join(os.getcwd(), TEMP_DOWNLOAD, file_name)

        @after_this_request
        def cleanup(response):
            shutil.rmtree(TEMP_DOWNLOAD, ignore_errors=True)
            os.makedirs(TEMP_DOWNLOAD, exist_ok=True)
            return response

        return send_file(decrypted_file_path, as_attachment=True)

    except Exception as e:
        print(f"Decryption error: {e}")
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

    return process_download(file_name, chunks_urls, message_ids, key_hex, file_id=file_id)


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
                print(f"Failed to delete message {msg_id}: {e}")
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
