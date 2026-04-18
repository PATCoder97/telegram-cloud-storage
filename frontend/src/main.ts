import dayjs from "dayjs";
import localizedFormat from "dayjs/plugin/localizedFormat";
import relativeTime from "dayjs/plugin/relativeTime";
import duration from "dayjs/plugin/duration";
import type {
  PluginOptions,
  ToastOptions,
} from "vue-toastification/dist/types/types";

import "./css/styles.css";

dayjs.extend(localizedFormat);
dayjs.extend(relativeTime);
dayjs.extend(duration);

function prependStaticUrl(url: string): string {
  const prefix = (window.FileBrowser?.StaticURL || "").replace(/\/$/, "");
  const normalized = url.replace(/^\/+/, "");
  return prefix ? `${prefix}/${normalized}` : `/${normalized}`;
}

async function loadFrontendConfig() {
  const response = await fetch("/api/frontend-config.js", {
    credentials: "same-origin",
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`Failed to load frontend config: ${response.status}`);
  }

  const source = await response.text();
  new Function(source)();

  window.__prependStaticUrl = prependStaticUrl;

  const dynamicManifest = {
    name: window.FileBrowser.Name || "File Browser",
    short_name: window.FileBrowser.Name || "File Browser",
    icons: [
      {
        src: prependStaticUrl("/img/icons/android-chrome-192x192.png"),
        sizes: "192x192",
        type: "image/png",
      },
      {
        src: prependStaticUrl("/img/icons/android-chrome-512x512.png"),
        sizes: "512x512",
        type: "image/png",
      },
    ],
    start_url: window.location.origin + window.FileBrowser.BaseURL,
    display: "standalone",
    background_color: "#ffffff",
    theme_color: window.FileBrowser.Color || "#455a64",
  };

  const blob = new Blob([JSON.stringify(dynamicManifest)], {
    type: "application/json",
  });
  const manifestURL = URL.createObjectURL(blob);
  document
    .querySelector("#manifestPlaceholder")
    ?.setAttribute("href", manifestURL);
}

async function bootstrap() {
  await loadFrontendConfig();

  const [
    { disableExternal },
    { createApp },
    { default: VueNumberInput },
    { default: VueLazyload },
    toastModule,
    { default: createPinia },
    { default: router },
    i18nModule,
    { default: App },
    { default: CustomToast },
  ] = await Promise.all([
    import("@/utils/constants"),
    import("vue"),
    import("@chenfengyuan/vue-number-input"),
    import("vue-lazyload"),
    import("vue-toastification"),
    import("@/stores"),
    import("@/router"),
    import("@/i18n"),
    import("@/App.vue"),
    import("@/components/CustomToast.vue"),
  ]);

  const { default: Toast, POSITION, useToast } = toastModule;
  const { default: i18n, isRtl } = i18nModule;

  const pinia = createPinia(router);
  const app = createApp(App);

  app.component(VueNumberInput.name || "vue-number-input", VueNumberInput);
  app.use(VueLazyload);
  app.use(Toast, {
    transition: "Vue-Toastification__bounce",
    maxToasts: 10,
    newestOnTop: true,
  } satisfies PluginOptions);

  app.use(i18n);
  app.use(pinia);
  app.use(router);

  app.mixin({
    mounted() {
      this.$el.__vue__ = this;
    },
  });

  app.directive("focus", {
    mounted: async (el) => {
      el.focus();
    },
  });

  const toastConfig = {
    position: POSITION.BOTTOM_CENTER,
    timeout: 4000,
    closeOnClick: true,
    pauseOnFocusLoss: true,
    pauseOnHover: true,
    draggable: true,
    draggablePercent: 0.6,
    showCloseButtonOnHover: false,
    hideProgressBar: false,
    closeButton: "button",
    icon: true,
  } satisfies ToastOptions;

  app.provide("$showSuccess", (message: string) => {
    const $toast = useToast();
    $toast.success(
      {
        component: CustomToast,
        props: {
          message: message,
        },
      },
      { ...toastConfig, rtl: isRtl() }
    );
  });

  app.provide("$showError", (error: Error | string, displayReport = true) => {
    const $toast = useToast();
    $toast.error(
      {
        component: CustomToast,
        props: {
          message: (error as Error).message || error,
          isReport: !disableExternal && displayReport,
          reportText: i18n.global.t("buttons.reportIssue"),
        },
      },
      {
        ...toastConfig,
        timeout: 0,
        rtl: isRtl(),
      }
    );
  });

  router.isReady().then(() => app.mount("#app"));
}

bootstrap().catch((error) => {
  console.error("Failed to bootstrap frontend", error);
});
