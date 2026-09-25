// Lossless JSON checks shared by the mux client's uplink boundary (upstream
// packages/typert/protocol/src/json-value.ts). A value that would be coerced
// or dropped by JSON.stringify must not enter the wire: the Host decodes each
// uplink item against the method's codec and cannot tell a lossy value from the
// one the Client meant to send.

/** Test whether a value crosses JSON transport without coercion or omission. */
export function isRemoteJsonValue(value: unknown): boolean {
  return visitJsonValue(value, new Set<object>());
}

/**
 * Test whether a value may travel as one uplink item: a lossless JSON value, or
 * a top-level `undefined`, which the wire carries as an `item` frame without
 * `value`. Nested `undefined`, `NaN`, and infinities stay rejected.
 */
export function isRemoteUplinkItem(value: unknown): boolean {
  return value === undefined || isRemoteJsonValue(value);
}

function visitJsonValue(value: unknown, ancestors: Set<object>): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value) && !Object.is(value, -0);
  if (typeof value !== "object") return false;
  const node = value as object;
  if (ancestors.has(node)) return false;
  ancestors.add(node);
  try {
    if (Array.isArray(node)) {
      if (Object.getPrototypeOf(node) !== Array.prototype
        || Reflect.ownKeys(node).length !== node.length + 1) return false;
      for (let index = 0; index < node.length; index++) {
        if (!Object.prototype.hasOwnProperty.call(node, index)
          || !visitJsonValue(node[index], ancestors)) return false;
      }
      return true;
    }
    const prototype: unknown = Object.getPrototypeOf(node);
    if (prototype !== Object.prototype && prototype !== null) return false;
    for (const key of Reflect.ownKeys(node)) {
      if (typeof key !== "string") return false;
      const descriptor = Object.getOwnPropertyDescriptor(node, key);
      if (descriptor?.enumerable !== true
        || !visitJsonValue(Reflect.get(node, key), ancestors)) return false;
    }
    return true;
  } finally {
    ancestors.delete(node);
  }
}
