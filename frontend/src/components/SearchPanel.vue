<script setup lang="ts">
defineProps<{
  modelValue: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
}>();
</script>

<template>
  <div class="searchbox fb-search">
    <span class="material-symbol">search</span>
    <input
      :value="modelValue"
      type="text"
      placeholder="Search..."
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
  </div>
</template>
