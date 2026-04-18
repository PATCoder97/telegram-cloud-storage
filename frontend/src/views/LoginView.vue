<script setup lang="ts">
defineProps<{
  appName: string;
  username: string;
  password: string;
  loading: boolean;
  error: string;
}>();

const emit = defineEmits<{
  "update:username": [value: string];
  "update:password": [value: string];
  submit: [];
}>();
</script>

<template>
  <section class="login-screen fb-login">
    <div id="login" class="login-card">
      <form @submit.prevent="emit('submit')">
        <div class="brand-mark">FB</div>
        <h1>{{ appName }}</h1>
        <p>Telegram-backed storage with a File Browser style shell</p>
        <div v-if="error" class="wrong">{{ error }}</div>
        <input
          :value="username"
          autofocus
          class="input input--block"
          type="text"
          placeholder="Username"
          autocomplete="username"
          @input="emit('update:username', ($event.target as HTMLInputElement).value)"
        />
        <input
          :value="password"
          class="input input--block"
          type="password"
          placeholder="Password"
          autocomplete="current-password"
          @input="emit('update:password', ($event.target as HTMLInputElement).value)"
        />
        <input class="button button--block" type="submit" :value="loading ? 'Signing in...' : 'Login'" />
      </form>
    </div>
  </section>
</template>
