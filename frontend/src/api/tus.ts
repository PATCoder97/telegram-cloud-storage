import * as tus from "tus-js-client";
import { joinBaseURL, tusEndpoint, tusSettings, origin } from "@/utils/constants";
import { useAuthStore } from "@/stores/auth";
import { removePrefix } from "@/api/utils";
import { shouldFallbackFromTus } from "./uploadFallback";

const RETRY_BASE_DELAY = 1000;
const RETRY_MAX_DELAY = 20000;
const CURRENT_UPLOAD_LIST: { [key: string]: tus.Upload } = {};
type TusUploadError = Error & { status?: number };

export async function upload(
  filePath: string,
  content: ApiContent = "",
  overwrite = false,
  onupload: any
) {
  if (!tusSettings) {
    // Shouldn't happen as we check for tus support before calling this function
    throw new Error("Tus.io settings are not defined");
  }

  filePath = removePrefix(filePath);
  const resourcePath = `${tusEndpoint}${filePath}?override=${overwrite}`;

  const authStore = useAuthStore();

  // Exit early because of typescript, tus content can't be a string
  if (content === "") {
    return false;
  }
  return new Promise<void | string>((resolve, reject) => {
    const upload = new tus.Upload(content, {
      endpoint: new URL(joinBaseURL(resourcePath), origin).toString(),
      chunkSize: tusSettings.chunkSize,
      retryDelays: computeRetryDelays(tusSettings),
      parallelUploads: 1,
      storeFingerprintForResuming: false,
      headers: {
        "X-Auth": authStore.jwt,
      },
      onShouldRetry: function (err) {
        const status = err.originalResponse
          ? err.originalResponse.getStatus()
          : 0;

        // Fail fast for known non-retryable server responses.
        if (status === 409 || shouldFallbackFromTus({ status })) {
          return false;
        }

        return true;
      },
      onError: function (error: Error | tus.DetailedError) {
        delete CURRENT_UPLOAD_LIST[filePath];

        if (error.message === "Upload aborted") {
          return reject(error);
        }

        const status =
          error instanceof tus.DetailedError && error.originalResponse !== null
            ? error.originalResponse.getStatus()
            : 0;
        const message =
          error instanceof tus.DetailedError
            ? error.originalResponse === null
              ? "000 No connection"
              : error.originalResponse.getBody()
            : "Upload failed";
        const uploadError = new Error(message) as TusUploadError;
        uploadError.name = "TusUploadError";
        uploadError.status = status;

        console.error(error);

        reject(uploadError);
      },
      onProgress: function (bytesUploaded) {
        if (typeof onupload === "function") {
          onupload({ loaded: bytesUploaded });
        }
      },
      onSuccess: function () {
        delete CURRENT_UPLOAD_LIST[filePath];
        resolve();
      },
    });
    CURRENT_UPLOAD_LIST[filePath] = upload;
    upload.start();
  });
}

function computeRetryDelays(tusSettings: TusSettings): number[] | undefined {
  if (!tusSettings.retryCount || tusSettings.retryCount < 1) {
    // Disable retries altogether
    return undefined;
  }
  // The tus client expects our retries as an array with computed backoffs
  // E.g.: [0, 3000, 5000, 10000, 20000]
  const retryDelays = [];
  let delay = 0;

  for (let i = 0; i < tusSettings.retryCount; i++) {
    retryDelays.push(Math.min(delay, RETRY_MAX_DELAY));
    delay =
      delay === 0 ? RETRY_BASE_DELAY : Math.min(delay * 2, RETRY_MAX_DELAY);
  }

  return retryDelays;
}

export async function useTus(content: ApiContent) {
  // This custom Flask backend does not implement `/api/tus`, so forcing
  // direct uploads avoids the guaranteed 404 before our fallback kicks in.
  void content;
  return false;
}

export function abortAllUploads() {
  for (const filePath in CURRENT_UPLOAD_LIST) {
    if (CURRENT_UPLOAD_LIST[filePath]) {
      CURRENT_UPLOAD_LIST[filePath].abort(true);
      CURRENT_UPLOAD_LIST[filePath].options!.onError!(
        new Error("Upload aborted")
      );
    }
    delete CURRENT_UPLOAD_LIST[filePath];
  }
}
