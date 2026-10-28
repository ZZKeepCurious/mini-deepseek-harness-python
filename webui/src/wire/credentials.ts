// `credentials` Remote namespace (packages/api/settings-controller).
// Values only travel inward: describe answers {configured, source?, writable}
// per ref and never echoes the stored secret. set/unset answer no value; every
// refusal is `credential/rejected`.

import { rpc } from "./rpc";

/** One credential reference's state, without the secret itself. */
export interface CredentialView {
  configured: boolean;
  /** Where the value comes from (`env`/`file`/...); omitted when unset. */
  source?: string;
  writable?: boolean;
}

export type CredentialsDescribe = Record<string, CredentialView>;

export async function describeCredentials(refs: string[]): Promise<CredentialsDescribe> {
  return rpc<CredentialsDescribe>("credentials/describe", { refs });
}

export async function setCredential(ref: string, value: string): Promise<void> {
  await rpc("credentials/set", { ref, value });
}

export async function unsetCredential(ref: string): Promise<void> {
  await rpc("credentials/unset", { ref });
}

export const credentialsServices = {
  describe: describeCredentials,
  set: setCredential,
  unset: unsetCredential,
};
