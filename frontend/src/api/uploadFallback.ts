const TUS_FALLBACK_STATUS_CODES = new Set([404, 405, 410, 501]);

type UploadErrorLike = {
  status?: unknown;
  message?: unknown;
};

export function shouldFallbackFromTus(error: unknown): boolean {
  if (!error || typeof error !== "object") {
    return false;
  }

  const { status, message } = error as UploadErrorLike;
  if (typeof status === "number") {
    return TUS_FALLBACK_STATUS_CODES.has(status);
  }

  if (typeof message !== "string") {
    return false;
  }

  const normalized = message.trim().toLowerCase();
  return (
    normalized === "not found" ||
    normalized === "404 not found" ||
    normalized === "405 method not allowed" ||
    normalized === "501 not implemented"
  );
}
