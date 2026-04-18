function normalizeBasePath(value: unknown): string {
  if (typeof value !== "string") return "";

  const raw = value.trim();
  if (raw === "" || raw === "/") return "";

  const trimmed = raw.replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

function normalizeStaticPath(value: unknown): string {
  if (typeof value !== "string") return "";

  const raw = value.trim();
  if (raw === "" || raw === "/") return "";

  return raw.replace(/\/+$/, "");
}

function normalizeAppPath(path: unknown): string {
  if (typeof path !== "string") return "";

  const raw = path.trim();
  if (raw === "") return "";

  return raw.startsWith("/") ? raw : `/${raw}`;
}

const name: string = window.FileBrowser.Name || "File Browser";
const disableExternal: boolean = window.FileBrowser.DisableExternal;
const disableUsedPercentage: boolean = window.FileBrowser.DisableUsedPercentage;
const baseURL: string = normalizeBasePath(window.FileBrowser.BaseURL);
const staticURL: string = normalizeStaticPath(window.FileBrowser.StaticURL);
const recaptcha: string = window.FileBrowser.ReCaptcha;
const recaptchaKey: string = window.FileBrowser.ReCaptchaKey;
const signup: boolean = window.FileBrowser.Signup;
const version: string = window.FileBrowser.Version;
const logoURL = `${staticURL}/img/logo.svg`;
const noAuth: boolean = window.FileBrowser.NoAuth;
const authMethod = window.FileBrowser.AuthMethod;
const logoutPage: string = window.FileBrowser.LogoutPage;
const loginPage: boolean = window.FileBrowser.LoginPage;
const theme: UserTheme = window.FileBrowser.Theme;
const enableThumbs: boolean = window.FileBrowser.EnableThumbs;
const resizePreview: boolean = window.FileBrowser.ResizePreview;
const enableExec: boolean = window.FileBrowser.EnableExec;
const tusSettings = window.FileBrowser.TusSettings;
const origin = window.location.origin;
const tusEndpoint = `/api/tus`;
const hideLoginButton = window.FileBrowser.HideLoginButton;

function joinBaseURL(path: string): string {
  return `${baseURL}${normalizeAppPath(path)}`;
}

export {
  name,
  disableExternal,
  disableUsedPercentage,
  baseURL,
  joinBaseURL,
  logoURL,
  recaptcha,
  recaptchaKey,
  signup,
  version,
  noAuth,
  authMethod,
  logoutPage,
  loginPage,
  theme,
  enableThumbs,
  resizePreview,
  enableExec,
  tusSettings,
  origin,
  tusEndpoint,
  hideLoginButton,
};
