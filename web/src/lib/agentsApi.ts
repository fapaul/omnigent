import { authenticatedFetch } from "@/lib/identity";

/** An agent installed with {@link installAgentBundle}. */
export interface InstalledAgent {
  id: string;
  name: string;
}

/**
 * Install (or replace) the caller's reusable agent from a `.tar.gz` bundle,
 * the same shape `omnigent agent add` uploads. The server validates the
 * bundle and never runs it; the agent then stays in the new-session picker.
 *
 * @param bundle - Gzipped tarball chosen by the user.
 * @returns The installed agent's id and name.
 * @throws Error carrying the server's message when the install is rejected.
 */
export async function installAgentBundle(bundle: File): Promise<InstalledAgent> {
  const form = new FormData();
  form.append("bundle", bundle, bundle.name);
  const res = await authenticatedFetch("/v1/agents", { method: "POST", body: form });
  const body = (await res.json().catch(() => null)) as
    (InstalledAgent & { error?: { message?: string }; detail?: string }) | null;
  if (!res.ok || body == null) {
    throw new Error(body?.error?.message ?? body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return { id: body.id, name: body.name };
}
