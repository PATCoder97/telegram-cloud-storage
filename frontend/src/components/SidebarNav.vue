<script setup lang="ts">
import type { User } from "../types";

defineProps<{
  user: User | null;
  appName: string;
}>();

const emit = defineEmits<{
  root: [];
  createFolder: [];
  upload: [];
  logout: [];
}>();
</script>

<template>
  <aside class="sidebar fb-sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-brand-icon">FB</div>
      <div class="sidebar-brand-copy">
        <strong>{{ appName }}</strong>
        <span>{{ user?.username }}</span>
      </div>
    </div>

    <nav class="sidebar-nav">
      <button class="sidebar-link active" @click="emit('root')">
        <span class="material-symbol">folder</span>
        <span>My files</span>
      </button>
      <button class="sidebar-link" @click="emit('createFolder')">
        <span class="material-symbol">create_new_folder</span>
        <span>New folder</span>
      </button>
      <button class="sidebar-link" @click="emit('upload')">
        <span class="material-symbol">note_add</span>
        <span>New file</span>
      </button>
    </nav>

    <div class="sidebar-footer">
      <button class="sidebar-link" @click="emit('logout')">
        <span class="material-symbol">logout</span>
        <span>Logout</span>
      </button>
    </div>
  </aside>
</template>
