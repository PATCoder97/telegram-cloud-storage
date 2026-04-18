<script setup lang="ts">
import type { BrowseResponse, FileItem, Folder } from "../types";

defineProps<{
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
  <div class="content fb-files">
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
          @click="emit('navigate', parentPath())"
        >
          <div class="entry-icon folder-icon"></div>
          <div class="entry-text">
            <strong>..</strong>
            <span>Go back</span>
          </div>
        </button>

        <article v-for="folder in visibleFolders" :key="folder.id" class="entry-card">
          <button class="entry-main" @click="emit('navigate', childPath(folder))">
            <div class="entry-icon folder-icon"></div>
            <div class="entry-text">
              <strong>{{ folder.name }}</strong>
              <span>Folder</span>
            </div>
          </button>
          <div class="entry-tools">
            <button @click="emit('renameFolder', folder)">Rename</button>
            <button class="danger" @click="emit('deleteFolder', folder)">Delete</button>
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
            <button v-if="file.status === 'Processing'" @click="emit('stopFile', file)">Stop</button>
            <template v-else-if="file.status === 'Error' || file.status === 'Stopped'">
              <button @click="emit('retryFile', file)">Retry</button>
              <button class="danger" @click="emit('deleteFile', file)">Delete</button>
            </template>
            <template v-else>
              <button @click="emit('downloadFile', file)">Download</button>
              <button @click="emit('togglePublicLink', file)">{{ file.public_token ? "Unshare" : "Share" }}</button>
              <button @click="emit('moveFile', file)">Move</button>
              <button class="danger" @click="emit('deleteFile', file)">Delete</button>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
