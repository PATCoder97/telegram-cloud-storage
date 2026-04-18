<script setup lang="ts">
import type { Breadcrumb } from "../types";

defineProps<{
  items: Breadcrumb[];
}>();

const emit = defineEmits<{
  navigate: [path: string];
}>();

function breadcrumbPath(items: Breadcrumb[], index: number) {
  const crumbs = items.slice(1, index + 1);
  return crumbs.map((crumb) => crumb.name).join("/");
}
</script>

<template>
  <div class="breadcrumbs fb-breadcrumbs">
    <button class="crumb home" @click="emit('navigate', '')">
      <span class="material-symbol">home</span>
    </button>
    <span v-for="(crumb, index) in items.slice(1)" :key="`${crumb.id}-${index}`" class="crumb-wrap">
      <span class="chevron">
        <span class="material-symbol">chevron_right</span>
      </span>
      <button class="crumb" @click="emit('navigate', breadcrumbPath(items, index + 1))">
        {{ crumb.name }}
      </button>
    </span>
  </div>
</template>
