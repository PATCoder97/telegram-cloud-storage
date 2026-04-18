<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import LayoutView from "./views/LayoutView.vue";
import LoginView from "./views/LoginView.vue";
import type { BrowseResponse, FileItem, Folder, User } from "./types";

type ApiEnvelope<T> = T & { ok: boolean; message?: string };
type FileBrowserConfig = { Name?: string };

const sessionChecked = ref(false);
const authenticated = ref(false);
const user = ref<User | null>(null);
const browse = ref<BrowseResponse | null>(null);
const search = ref("");
const statusText = ref("");
const loginUsername = ref("admin");
const loginPassword = ref("admin");
const loginError = ref("");
const loading = ref(false);
const uploadPercent = ref(0);
const uploadingName = ref("");
const appName = ((window as any).FileBrowser as FileBrowserConfig | undefined)?.Name || "File Browser";
let refreshTimer: number | null = null;

function currentFolderPath(): string {
  const pathname = window.location.pathname;
  if (pathname === "/" || pathname === "/files" || pathname === "/files/") return "";
  if (pathname.startsWith("/files/")) {
    return decodeURIComponent(pathname.replace(/^\/files\//, "").replace(/\/+$/, ""));
  }
  return "";
}

function updateUrl(folderPath: string) {
  const nextUrl = folderPath ? `/files/${encodeURI(folderPath)}` : "/files/";
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
      await loadBrowse(currentFolderPath(), false);
      if (window.location.pathname === "/login" || window.location.pathname === "/") {
        window.history.replaceState({}, "", "/files/");
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

async function loadBrowse(folderPath = "", replaceUrl = true) {
  if (!authenticated.value) return;
  const normalizedPath = folderPath.replace(/^\/+|\/+$/g, "");
  const url = normalizedPath ? `/api/browse-path/${encodeURI(normalizedPath)}` : "/api/browse-path";
  const data = await api<BrowseResponse>(url);
  browse.value = data;
  if (replaceUrl) {
    updateUrl(data.current_path || "");
  }
  syncAutoRefresh();
}

async function submitLogin() {
  loading.value = true;
  loginError.value = "";
  try {
    const data = await api<{ authenticated: boolean; user: User }>("/api/session", {
      method: "POST",
      body: JSON.stringify({ username: loginUsername.value, password: loginPassword.value })
    });
    authenticated.value = data.authenticated;
    user.value = data.user;
    await loadBrowse(currentFolderPath(), false);
    window.history.replaceState({}, "", "/files/");
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
  await loadBrowse(browse.value?.current_path || "", false);
}

async function renameFolder(folder: Folder) {
  const name = window.prompt("Rename folder", folder.name);
  if (!name || name === folder.name) return;
  await api(`/api/folders/${folder.id}`, {
    method: "PATCH",
    body: JSON.stringify({ name })
  });
  await loadBrowse(browse.value?.current_path || "", false);
}

async function deleteFolder(folder: Folder) {
  if (!window.confirm(`Delete folder "${folder.name}" and move files to root?`)) return;
  await api(`/api/folders/${folder.id}`, { method: "DELETE" });
  await loadBrowse(browse.value?.current_path || "", false);
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
  await loadBrowse(browse.value?.current_path || "", false);
}

async function togglePublicLink(file: FileItem) {
  const data = await api<{ public_url: string | null }>(`/api/files/${file.id}/public-link`, { method: "POST" });
  await loadBrowse(browse.value?.current_path || "", false);
  if (data.public_url) {
    window.prompt("Public link", data.public_url);
  }
}

async function retryFile(file: FileItem) {
  await api(`/api/files/${file.id}/retry`, { method: "POST" });
  await loadBrowse(browse.value?.current_path || "", false);
}

async function stopFile(file: FileItem) {
  await api(`/api/files/${file.id}/stop`, { method: "POST" });
  await loadBrowse(browse.value?.current_path || "", false);
}

async function deleteFile(file: FileItem) {
  if (!window.confirm(`Delete "${file.file_name}"?`)) return;
  await api(`/api/files/${file.id}`, { method: "DELETE" });
  await loadBrowse(browse.value?.current_path || "", false);
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
  if (target) target.value = "";
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
      await loadBrowse(browse.value?.current_path || "", false);
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

function fileIcon(file: FileItem) {
  if (file.status === "Processing") return "spinner";
  if (file.status === "Error") return "error";
  if (file.status === "Stopped") return "stop";
  if (["jpg", "jpeg", "png", "gif", "webp"].includes(file.extension)) return "image";
  if (["mp4", "mkv", "avi", "mov"].includes(file.extension)) return "video";
  if (["zip", "rar", "7z", "tar", "gz"].includes(file.extension)) return "archive";
  return "file";
}

function childPath(folder: Folder) {
  return browse.value?.current_path ? `${browse.value.current_path}/${folder.name}` : folder.name;
}

function parentPath() {
  const crumbs = (browse.value?.breadcrumbs || []).filter((crumb) => crumb.id !== null);
  return crumbs.slice(0, -1).map((crumb) => crumb.name).join("/");
}

const visibleFolders = computed(() => (browse.value?.folders || []).filter((folder) => folder.name.toLowerCase().includes(search.value.toLowerCase())));
const visibleFiles = computed(() => (browse.value?.files || []).filter((file) => file.file_name.toLowerCase().includes(search.value.toLowerCase())));
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
      loadBrowse(browse.value?.current_path || "", false).catch(() => undefined);
    }, 5000);
  }
}

watch(activeProcessing, () => syncAutoRefresh());

const handlePopState = () => {
  if (authenticated.value) {
    loadBrowse(currentFolderPath(), false).catch(() => undefined);
  }
};

onMounted(() => {
  window.addEventListener("popstate", handlePopState);
  loadSession();
});

onBeforeUnmount(() => {
  stopAutoRefresh();
  window.removeEventListener("popstate", handlePopState);
});
</script>

<template>
  <div v-if="sessionChecked" class="shell">
    <LoginView
      v-if="!authenticated"
      :app-name="appName"
      :username="loginUsername"
      :password="loginPassword"
      :loading="loading"
      :error="loginError"
      @update:username="loginUsername = $event"
      @update:password="loginPassword = $event"
      @submit="submitLogin"
    />

    <LayoutView
      v-else
      :app-name="appName"
      :user="user"
      :browse="browse"
      :search="search"
      :status-text="statusText"
      :uploading-name="uploadingName"
      :upload-percent="uploadPercent"
      :visible-folders="visibleFolders"
      :visible-files="visibleFiles"
      :file-icon="fileIcon"
      :display-time="displayTime"
      :child-path="childPath"
      :parent-path="parentPath"
      @update:search="search = $event"
      @root="loadBrowse('')"
      @create-folder="createFolder"
      @upload="triggerUpload"
      @logout="logout"
      @navigate="loadBrowse($event)"
      @rename-folder="renameFolder"
      @delete-folder="deleteFolder"
      @stop-file="stopFile"
      @retry-file="retryFile"
      @delete-file="deleteFile"
      @download-file="downloadFile"
      @toggle-public-link="togglePublicLink"
      @move-file="moveFile"
    />

    <input id="file-input" class="hidden-input" type="file" multiple @change="handleFileInputChange" />
  </div>
</template>
