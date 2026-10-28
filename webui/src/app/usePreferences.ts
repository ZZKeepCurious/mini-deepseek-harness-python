// React binding for the browser preference record (app/uiPreferences).

import { useEffect, useState } from "react";
import {
  loadPreferences,
  subscribePreferences,
  updatePreferences,
  type UiPreferences,
} from "./uiPreferences";

export interface PreferencesBinding {
  preferences: UiPreferences;
  update: (patch: Partial<UiPreferences>) => void;
}

export function usePreferences(): PreferencesBinding {
  const [preferences, setPreferences] = useState<UiPreferences>(() => loadPreferences());
  useEffect(() => subscribePreferences(setPreferences), []);
  return { preferences, update: updatePreferences };
}
