<script setup lang="ts">
import BreadcrumbsBar from "../components/BreadcrumbsBar.vue";
import SearchPanel from "../components/SearchPanel.vue";
import SidebarNav from "../components/SidebarNav.vue";
import FilesView from "./FilesView.vue";
import type { BrowseResponse, FileItem, Folder, User } from "../types";

defineProps<{
  appName: string;
  user: User | null;
  browse: BrowseResponse | null;
  search: string;
  statusText: string;
  uploadingName: string;
  uploadPercent: number;
  visibleFolders: Folder[];
  visibleFiles: FileItem[];
  fileIcon: (file: FileItem) => string;
  displayTime: (file: FileItem) => string;
  childPath: (folder: Folder) => string;
  parentPath: () => string;
}>();

const emit = defineEmits<{
  "update:search": [value: string];
  root: [];
  createFolder: [];
  upload: [];
  logout: [];
  navigate: [path: string];
  renameFolder: [folder: Folder];
  deleteFolder: [folder: Folder];
  stopFile: [file: FileItem];
  retryFile: [file: FileItem];
  deleteFile: [file: FileItem];
  downloadFile: [file: FileItem];
  togglePublicLink: [file: FileItem];
  moveFile: [file: FileItem];
}>();
</script>

<template>
  <section class="browser-shell fb-layout">
    <SidebarNav :user="user" :app-name="appName" @root="emit('root')" @create-folder="emit('createFolder')" @upload="emit('upload')" @logout="emit('logout')" />

    <main class="workspace">
      <header class="topbar fb-topbar">
        <SearchPanel :model-value="search" @update:model-value="emit('update:search', $event)" />
        <div class="topbar-actions">
          <button class="topbar-button" @click="emit('upload')">Upload</button>
          <button class="topbar-button" @click="emit('createFolder')">Folder</button>
        </div>
      </header>

      <div class="content-wrap">
        <BreadcrumbsBar :items="browse?.breadcrumbs || []" @navigate="emit('navigate', $event)" />
        <FilesView
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
          @navigate="emit('navigate', $event)"
          @rename-folder="emit('renameFolder', $event)"
          @delete-folder="emit('deleteFolder', $event)"
          @stop-file="emit('stopFile', $event)"
          @retry-file="emit('retryFile', $event)"
          @delete-file="emit('deleteFile', $event)"
          @download-file="emit('downloadFile', $event)"
          @toggle-public-link="emit('togglePublicLink', $event)"
          @move-file="emit('moveFile', $event)"
        />
      </div>
    </main>
  </section>
</template>
