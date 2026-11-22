import { fileIo } from '@kit.CoreFileKit';
import dm from 'libdm.so';

export const SCENE_HEIGHTS: number[] = [360, 432, 480, 540, 600, 720, 900, 1080];

export class DebugSettings {
  // Unset values retain the device's existing runtime/env defaults.
  sceneHeight: number = -1;
  renderOptions: number = -1;
  backgroundAutosave: boolean = true;
}

export function loadDebugSettings(root: string): DebugSettings {
  const result = new DebugSettings();
  try {
    const saved = JSON.parse(fileIo.readTextSync(root + '/debug-settings.json')) as DebugSettings;
    if (SCENE_HEIGHTS.indexOf(saved.sceneHeight) >= 0) { result.sceneHeight = saved.sceneHeight; }
    if (Number.isInteger(saved.renderOptions) && saved.renderOptions >= 0 && saved.renderOptions <= 3) {
      result.renderOptions = saved.renderOptions;
    }
    if (typeof saved.backgroundAutosave === 'boolean') { result.backgroundAutosave = saved.backgroundAutosave; }
  } catch (e) { }
  return result;
}

export function saveDebugSettings(root: string, settings: DebugSettings): boolean {
  try {
    const path = root + '/debug-settings.json';
    const file = fileIo.openSync(path + '.tmp', fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
    try { fileIo.writeSync(file.fd, JSON.stringify(settings)); fileIo.fsyncSync(file.fd); }
    finally { fileIo.closeSync(file); }
    fileIo.renameSync(path + '.tmp', path);
    return true;
  } catch (e) { return false; }
}

export function applyDebugSettings(settings: DebugSettings): void {
  if (settings.sceneHeight >= 0) { dm.setSceneHeight(settings.sceneHeight); }
  if (settings.renderOptions >= 0) { dm.setRenderOptions(settings.renderOptions); }
  dm.setBackgroundAutosave(settings.backgroundAutosave);
}
