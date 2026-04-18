<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { BrowseResponse, FileItem, Folder, User } from "./types";

type ApiEnvelope<T> = T & { ok: boolean; message?: string };

const sessionChecked = ref(false);
const authenticated = ref(false);
const user = ref<User | null>(null);
const browse = ref<BrowseResponse | null>(null);
const search = ref("");
const statusText = ref("");
const loginForm = ref({ username: "admin", password: "admin" });
const loginError = ref("");
const loading = ref(false);
const uploadPercent = ref(0);
const uploadingName = ref("");
let refreshTimer: number | null = null;

function currentFolderId(): number | null {
  const match = window.location.pathname.match(/^\/folder\/(\d+)/);
  return match ? Number(match[1]) : null;
}

function updateUrl(folderId: number | null) {
  const nextUrl = folderId ? `/folder/${folderId}` : "/";
  if (window.location.pathname !== nextUrl) {
    window.history.pushState({}, "", nextUrl);
  }
}

async function api<T>(url: string, init?: RequestInit): Promise<ApiEnvelope<T>> {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers || {})
    },
    ...init
  });
  const data = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !data.ok) {
    throw new Error(data.message || "Request failed");
  }
  return data;
}

async function loadSession() {
  try {
    const data = await api<{ authenticated: boolean; user?: User }>("/api/session");
    authenticated.value = data.authenticated;
    user.value = data.user || null;
    if (authenticated.value) {
      await loadBrowse(currentFolderId(), false);
      if (window.location.pathname === "/login") {
        window.history.replaceState({}, "", "/");
      }
    } else if (window.location.pathname !== "/login") {
      window.history.replaceState({}, "", "/login");
    }
  } catch (error) {
    loginError.value = (error as Error).message;
  } finally {
    sessionChecked.value = true;
  }
}

async function loadBrowse(folderId: number | null, replaceUrl = true) {
  if (!authenticated.value) return;
  const url = folderId ? `/api/browse/${folderId}` : "/api/browse";
  const data = await api<BrowseResponse>(url);
  browse.value = data;
  if (replaceUrl) {
    updateUrl(folderId);
  }
  syncAutoRefresh();
}

async function submitLogin() {
  loading.value = true;
  loginError.value = "";
  try {
    const data = await api<{ authenticated: boolean; user: User }>("/api/session", {
      method: "POST",
      body: JSON.stringify(loginForm.value)
    });
    authenticated.value = data.authenticated;
    user.value = data.user;
    await loadBrowse(currentFolderId(), false);
    window.history.replaceState({}, "", "/");
  } catch (error) {
    loginError.value = (error as Error).message;
  } finally {
    loading.value = false;
  }
}

async function logout() {
  await api("/api/session", { method: "DELETE" });
  authenticated.value = false;
  user.value = null;
  browse.value = null;
  stopAutoRefresh();
  window.history.replaceState({}, "", "/login");
}

async function createFolder() {
  const name = window.prompt("Folder name");
  if (!name) return;
  await api("/api/folders", {
    method: "POST",
    body: JSON.stringify({ name, parent_id: browse.value?.folder_id ?? null })
  });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function renameFolder(folder: Folder) {
  const name = window.prompt("Rename folder", folder.name);
  if (!name || name === folder.name) return;
  await api(`/api/folders/${folder.id}`, {
    method: "PATCH",
    body: JSON.stringify({ name })
  });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function deleteFolder(folder: Folder) {
  if (!window.confirm(`Delete folder "${folder.name}" and move files to root?`)) return;
  await api(`/api/folders/${folder.id}`, { method: "DELETE" });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function moveFile(file: FileItem) {
  const options = [{ id: "root", name: "/" }, ...((browse.value?.all_user_folders || []).map((folder) => ({ id: String(folder.id), name: folder.name })))];
  const list = options.map((item) => `${item.id}: ${item.name}`).join("\n");
  const choice = window.prompt(`Move "${file.file_name}" to folder id:\n${list}`, "root");
  if (choice === null) return;
  await api(`/api/files/${file.id}/move`, {
    method: "POST",
    body: JSON.stringify({ target_folder_id: choice })
  });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function togglePublicLink(file: FileItem) {
  const data = await api<{ public_url: string | null }>(`/api/files/${file.id}/public-link`, { method: "POST" });
  await loadBrowse(browse.value?.folder_id ?? null, false);
  if (data.public_url) {
    window.prompt("Public link", data.public_url);
  }
}

async function retryFile(file: FileItem) {
  await api(`/api/files/${file.id}/retry`, { method: "POST" });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function stopFile(file: FileItem) {
  await api(`/api/files/${file.id}/stop`, { method: "POST" });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

async function deleteFile(file: FileItem) {
  if (!window.confirm(`Delete "${file.file_name}"?`)) return;
  await api(`/api/files/${file.id}`, { method: "DELETE" });
  await loadBrowse(browse.value?.folder_id ?? null, false);
}

function downloadFile(file: FileItem) {
  window.location.href = `/download/${file.id}`;
}

function triggerUpload() {
  const input = document.getElementById("file-input") as HTMLInputElement | null;
  input?.click();
}

function uploadFiles(files: FileList | null) {
  if (!files?.length) return;
  Array.from(files).forEach((file) => uploadFile(file));
}

function handleFileInputChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  uploadFiles(target?.files || null);
  if (target) {
    target.value = "";
  }
}

function uploadFile(file: File) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("folder_id", browse.value?.folder_id ? String(browse.value.folder_id) : "root");
  uploadingName.value = file.name;
  uploadPercent.value = 0;
  statusText.value = "Uploading file...";

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/files/upload", true);
  xhr.withCredentials = true;
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    uploadPercent.value = Math.round((event.loaded / event.total) * 100);
  });
  xhr.onload = async () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      statusText.value = "Uploaded. Processing in background...";
      await loadBrowse(browse.value?.folder_id ?? null, false);
      syncAutoRefresh();
      window.setTimeout(() => {
        statusText.value = "";
        uploadPercent.value = 0;
      }, 1800);
    } else {
      statusText.value = "Upload failed";
    }
  };
  xhr.onerror = () => {
    statusText.value = "Upload failed";
  };
  xhr.send(formData);
}

function displayTime(file: FileItem) {
  if (file.status === "Processing") return "Processing";
  if (file.status === "Error") return file.error_message || "Upload error";
  if (file.status === "Stopped") return "Stopped";
  return file.formatted_size;
}

function isFolderVisible(folder: Folder) {
  return folder.name.toLowerCase().includes(search.value.toLowerCase());
}

function isFileVisible(file: FileItem) {
  const term = search.value.toLowerCase();
  return file.file_name.toLowerCase().includes(term);
}

function folderIcon() {
  return "folder";
}

function fileIcon(file: FileItem) {
  if (file.status === "Processing") return "spinner";
  if (file.status === "Error") return "error";
  if (file.status === "Stopped") return "stop";
  if (["jpg", "jpeg", "png", "gif", "webp"].includes(file.extension)) return "image";
  if (["mp4", "mkv", "avi", "mov"].includes(file.extension)) return "video";
  if (["zip", "rar", "7z", "tar", "gz"].includes(file.extension)) return "archive";
  return "file";
}

const visibleFolders = computed(() => (browse.value?.folders || []).filter(isFolderVisible));
const visibleFiles = computed(() => (browse.value?.files || []).filter(isFileVisible));
const activeProcessing = computed(() => (browse.value?.files || []).some((file) => file.status === "Processing"));

function stopAutoRefresh() {
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function syncAutoRefresh() {
  stopAutoRefresh();
  if (activeProcessing.value) {
    refreshTimer = window.setTimeout(() => {
      loadBrowse(browse.value?.folder_id ?? null, false).catch(() => undefined);
    }, 5000);
  }
}

watch(activeProcessing, () => syncAutoRefresh());

window.addEventListener("popstate", () => {
  if (authenticated.value) {
    loadBrowse(currentFolderId(), false).catch(() => undefined);
  }
});

onMounted(loadSession);
onBeforeUnmount(stopAutoRefresh);
</script>

<template>
  <div v-if="sessionChecked" class="shell">
    <section v-if="!authenticated" class="login-screen">
      <div class="login-card">
        <div class="brand-mark">FB</div>
        <h1>File Browser</h1>
        <p>Telegram-backed storage with a File Browser style shell</p>
        <form class="login-form" @submit.prevent="submitLogin">
          <input v-model="loginForm.username" type="text" placeholder="Username" autocomplete="username" />
          <input v-model="loginForm.password" type="password" placeholder="Password" autocomplete="current-password" />
          <button :disabled="loading" type="submit">{{ loading ? "Signing in..." : "Login" }}</button>
        </form>
        <p v-if="loginError" class="form-error">{{ loginError }}</p>
      </div>
    </section>

    <section v-else class="browser-shell">
      <aside class="sidebar">
        <div class="sidebar-brand">
          <div class="sidebar-brand-icon">FB</div>
          <div>
            <strong>Telegram Browser</strong>
            <span>{{ user?.username }}</span>
          </div>
        </div>

        <nav class="sidebar-nav">
          <button class="sidebar-link active" @click="loadBrowse(null)">
            <span class="link-icon">[]</span>
            <span>My files</span>
          </button>
          <button class="sidebar-link" @click="createFolder">
            <span class="link-icon">+</span>
            <span>New folder</span>
          </button>
          <button class="sidebar-link" @click="triggerUpload">
            <span class="link-icon">^</span>
            <span>New file</span>
          </button>
        </nav>

        <div class="sidebar-footer">
          <button class="sidebar-link" @click="logout">
            <span class="link-icon">x</span>
            <span>Logout</span>
          </button>
        </div>
      </aside>

      <main class="workspace">
        <header class="topbar">
          <div class="searchbox">
            <span>o</span>
            <input v-model="search" type="text" placeholder="Search..." />
          </div>
          <div class="topbar-actions">
            <button class="topbar-button" @click="triggerUpload">Upload</button>
            <button class="topbar-button" @click="createFolder">Folder</button>
          </div>
        </header>

        <div class="content">
          <div class="breadcrumbs">
            <button
              v-for="crumb in browse?.breadcrumbs || []"
              :key="String(crumb.id)"
              class="crumb"
              @click="loadBrowse(crumb.id)"
            >
              {{ crumb.name }}
            </button>
          </div>

          <div v-if="statusText" class="upload-banner">
            <strong>{{ uploadingName }}</strong>
            <span>{{ statusText }}</span>
            <div class="progress">
              <div class="progress-fill" :style="{ width: `${uploadPercent}%` }"></div>
            </div>
          </div>

          <section class="section-block">
            <div class="section-title">Folders</div>
            <div class="card-grid">
              <button
                v-if="browse?.parent_folder_id !== null"
                class="entry-card back-card"
                @click="loadBrowse(browse?.parent_folder_id ?? null)"
              >
                <div class="entry-icon folder-icon"></div>
                <div class="entry-text">
                  <strong>..</strong>
                  <span>Go back</span>
                </div>
              </button>

              <article v-for="folder in visibleFolders" :key="folder.id" class="entry-card">
                <button class="entry-main" @click="loadBrowse(folder.id)">
                  <div class="entry-icon folder-icon"></div>
                  <div class="entry-text">
                    <strong>{{ folder.name }}</strong>
                    <span>Folder</span>
                  </div>
                </button>
                <div class="entry-tools">
                  <button @click="renameFolder(folder)">Rename</button>
                  <button class="danger" @click="deleteFolder(folder)">Delete</button>
                </div>
              </article>
            </div>
          </section>

          <section class="section-block">
            <div class="section-title">Files</div>
            <div class="card-grid">
              <article v-for="file in visibleFiles" :key="file.id" class="entry-card">
                <div class="entry-main">
                  <div class="entry-icon" :class="`${fileIcon(file)}-icon`"></div>
                  <div class="entry-text">
                    <strong :title="file.file_name">{{ file.file_name }}</strong>
                    <span :class="{ danger: file.status === 'Error' }">{{ displayTime(file) }}</span>
                  </div>
                </div>
                <div class="entry-tools">
                  <button v-if="file.status === 'Processing'" @click="stopFile(file)">Stop</button>
                  <template v-else-if="file.status === 'Error' || file.status === 'Stopped'">
                    <button @click="retryFile(file)">Retry</button>
                    <button class="danger" @click="deleteFile(file)">Delete</button>
                  </template>
                  <template v-else>
                    <button @click="downloadFile(file)">Download</button>
                    <button @click="togglePublicLink(file)">{{ file.public_token ? "Unshare" : "Share" }}</button>
                    <button @click="moveFile(file)">Move</button>
                    <button class="danger" @click="deleteFile(file)">Delete</button>
                  </template>
                </div>
              </article>
            </div>
          </section>
        </div>
      </main>

      <input id="file-input" class="hidden-input" type="file" multiple @change="handleFileInputChange" />
    </section>
  </div>
</template>
