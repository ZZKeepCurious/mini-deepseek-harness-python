/** Listing badge from `catalogModelInfo` / discoverModels (upstream tone + i18n text). */

export interface ModelBadge {
  tone?: string;
  text?: Record<string, string>;
}

export function modelBadgeLabel(badge: ModelBadge | undefined): string | undefined {
  if (badge?.text === undefined) return undefined;
  const text = badge.text;
  return text.zh ?? text.en ?? Object.values(text)[0];
}
