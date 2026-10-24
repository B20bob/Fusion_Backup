"""
FusionBackup - copies every design in your Autodesk Fusion account to local
storage as .f3d (full Fusion archive, with timeline/history) and .step.

- Walks every hub, project and folder you have access to.
- Mirrors the cloud folder structure on disk:
      <Backup folder>/<Project>/<Folder>/<Subfolder>/<Design>.f3d / .step
- Incremental: remembers each design's version number in backup_state.json,
  so re-runs only export designs that changed since the last backup.
- Never saves or modifies anything in the cloud. Designs are opened read-only
  and closed without saving.
- Writes backup_log.txt in the backup folder with every success / failure.

Run it from Fusion: Utilities > Add-Ins > Scripts and Add-Ins > FusionBackup > Run.
"""

import adsk.core
import adsk.fusion
import traceback
import os
import re
import json
import datetime

# ----------------------------------------------------------------- settings --
EXPORT_F3D = True
EXPORT_STEP = True
MAX_NAME_LEN = 80          # keeps Windows paths under the 260-char limit
OPEN_RETRIES = 2           # retries if a design fails to download/open
# -----------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SETTINGS_FILE = os.path.join(SCRIPT_DIR, 'settings.json')
STATE_NAME = 'backup_state.json'
LOG_NAME = 'backup_log.txt'

_log_fh = None


def log(msg):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = '[{}] {}'.format(stamp, msg)
    if _log_fh:
        _log_fh.write(line + '\n')
        _log_fh.flush()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name or '')
    name = name.strip().rstrip('.')
    if len(name) > MAX_NAME_LEN:
        name = name[:MAX_NAME_LEN].rstrip()
    return name or '_'


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def items(collection):
    """Iterate any Fusion API collection safely."""
    for i in range(collection.count):
        yield collection.item(i)


def collect_files(folder, rel_parts, out, skipped):
    """Recursively gather (dataFile, relative folder parts) for Fusion designs."""
    adsk.doEvents()
    for df in items(folder.dataFiles):
        ext = (df.fileExtension or '').lower()
        if ext == 'f3d':
            out.append((df, list(rel_parts)))
        else:
            skipped[ext or 'unknown'] = skipped.get(ext or 'unknown', 0) + 1
    for sub in items(folder.dataFolders):
        collect_files(sub, rel_parts + [safe_name(sub.name)], out, skipped)


def find_open_doc(app, data_file_id):
    """Return a document the user already has open for this file, if any."""
    for doc in items(app.documents):
        try:
            if doc.dataFile and doc.dataFile.id == data_file_id:
                return doc
        except Exception:
            pass
    return None


def open_design(app, df):
    last_err = None
    for _ in range(OPEN_RETRIES + 1):
        try:
            doc = app.documents.open(df, False)   # open hidden
            if doc:
                return doc
        except Exception as e:
            last_err = e
            adsk.doEvents()
    raise RuntimeError('could not open: {}'.format(last_err))


def export_design(doc, base_path):
    design = adsk.fusion.Design.cast(
        doc.products.itemByProductType('DesignProductType'))
    if not design:
        raise RuntimeError('document has no design product')
    em = design.exportManager
    results = {}

    if EXPORT_F3D:
        try:
            opts = em.createFusionArchiveExportOptions(base_path + '.f3d')
            em.execute(opts)
            # Designs with linked (external) components may be written as .f3z
            if os.path.exists(base_path + '.f3d'):
                results['f3d'] = base_path + '.f3d'
            elif os.path.exists(base_path + '.f3z'):
                results['f3d'] = base_path + '.f3z'
            else:
                results['f3d_error'] = 'export ran but no file was written'
        except Exception as e:
            results['f3d_error'] = str(e)

    if EXPORT_STEP:
        try:
            opts = em.createSTEPExportOptions(base_path + '.step',
                                              design.rootComponent)
            em.execute(opts)
            if os.path.exists(base_path + '.step'):
                results['step'] = base_path + '.step'
            else:
                results['step_error'] = 'export ran but no file was written'
        except Exception as e:
            # Common for designs with no solid bodies (sketch-only, etc.)
            results['step_error'] = str(e)

    return results


def pick_backup_folder(ui):
    settings = load_json(SETTINGS_FILE, {})
    dlg = ui.createFolderDialog()
    dlg.title = 'Choose where to store your Fusion backup'
    default = settings.get('backup_root') or os.path.join(
        os.path.expanduser('~'), 'Documents')
    if os.path.isdir(default):
        dlg.initialDirectory = default
    if dlg.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    root = dlg.folder
    settings['backup_root'] = root
    try:
        save_json(SETTINGS_FILE, settings)
    except Exception:
        pass
    return root


def run(context):
    global _log_fh
    app = adsk.core.Application.get()
    ui = app.userInterface
    progress = None

    try:
        root = pick_backup_folder(ui)
        if not root:
            return
        os.makedirs(root, exist_ok=True)
        _log_fh = open(os.path.join(root, LOG_NAME), 'a', encoding='utf-8')
        state_path = os.path.join(root, STATE_NAME)
        state = load_json(state_path, {})

        log('=' * 70)
        log('Backup started -> {}'.format(root))

        # ---------------------------------------------------- 1. inventory --
        progress = ui.createProgressDialog()
        progress.cancelButtonText = 'Stop'
        progress.isBackgroundTranslucent = False
        progress.show('Fusion Backup', 'Scanning your projects...', 0, 1, 0)

        hubs = list(items(app.data.dataHubs))
        multi_hub = len(hubs) > 1
        to_process = []
        skipped_types = {}

        for hub in hubs:
            hub_parts = [safe_name(hub.name)] if multi_hub else []
            for proj in items(hub.dataProjects):
                progress.message = 'Scanning: {}'.format(proj.name)
                adsk.doEvents()
                if progress.wasCancelled:
                    log('Cancelled during scan.')
                    return
                try:
                    collect_files(proj.rootFolder,
                                  hub_parts + [safe_name(proj.name)],
                                  to_process, skipped_types)
                except Exception as e:
                    log('SCAN FAIL project "{}": {}'.format(proj.name, e))

        # Work out which designs actually need exporting
        pending = []
        for df, parts in to_process:
            prev = state.get(df.id)
            if prev and prev.get('version') == df.versionNumber and all(
                    os.path.exists(p) for p in prev.get('files', [])) and \
                    prev.get('files'):
                continue
            pending.append((df, parts))

        progress.hide()
        msg = ('Found {} designs in your account.\n'
               '{} are new or changed and will be exported.\n'
               '{} are already backed up and will be skipped.\n\n'
               'This can take a while for large accounts. Fusion will be busy '
               'until it finishes; you can press Stop at any time and pick up '
               'where it left off later.\n\nStart?').format(
                   len(to_process), len(pending),
                   len(to_process) - len(pending))
        if not pending:
            ui.messageBox('Everything is already backed up. Nothing to do.',
                          'Fusion Backup')
            log('Nothing to do; all {} designs current.'.format(len(to_process)))
            return
        if ui.messageBox(msg, 'Fusion Backup',
                         adsk.core.MessageBoxButtonTypes.OKCancelButtonType) \
                != adsk.core.DialogResults.DialogOK:
            return

        # ------------------------------------------------------ 2. export --
        progress.show('Fusion Backup', 'Starting...', 0, len(pending), 0)
        used_paths = set()
        ok = 0
        partial = 0
        failed = 0

        for i, (df, parts) in enumerate(pending):
            if progress.wasCancelled:
                log('Cancelled by user after {} designs.'.format(i))
                break
            progress.message = '({}/{}) {}'.format(i + 1, len(pending), df.name)
            progress.progressValue = i
            adsk.doEvents()

            folder = os.path.join(root, *parts)
            os.makedirs(folder, exist_ok=True)
            base = os.path.join(folder, safe_name(df.name))
            # Two designs with the same name in the same folder -> add an id
            if base.lower() in used_paths:
                base = '{} [{}]'.format(base, df.id[-6:])
            used_paths.add(base.lower())

            doc = None
            already_open = False
            try:
                doc = find_open_doc(app, df.id)
                already_open = doc is not None
                if not doc:
                    doc = open_design(app, df)
                res = export_design(doc, base)
                files = [v for k, v in res.items() if not k.endswith('_error')]
                errors = {k: v for k, v in res.items() if k.endswith('_error')}

                if files:
                    state[df.id] = {
                        'name': df.name,
                        'path': '/'.join(parts),
                        'version': df.versionNumber,
                        'files': files,
                        'exported': datetime.datetime.now().isoformat(
                            timespec='seconds'),
                    }
                    save_json(state_path, state)
                if errors and files:
                    partial += 1
                    log('PARTIAL {} (v{}): {}'.format(
                        '/'.join(parts + [df.name]), df.versionNumber, errors))
                elif errors:
                    failed += 1
                    log('FAIL    {} (v{}): {}'.format(
                        '/'.join(parts + [df.name]), df.versionNumber, errors))
                else:
                    ok += 1
                    log('OK      {} (v{})'.format(
                        '/'.join(parts + [df.name]), df.versionNumber))
            except Exception as e:
                failed += 1
                log('FAIL    {}: {}'.format('/'.join(parts + [df.name]), e))
            finally:
                if doc and not already_open:
                    try:
                        doc.close(False)   # never save back to the cloud
                    except Exception:
                        pass
                adsk.doEvents()

        progress.hide()
        progress = None

        skipped_txt = ''
        if skipped_types:
            skipped_txt = '\n\nNon-design files not exported: ' + ', '.join(
                '{} .{}'.format(n, ext) for ext, n in sorted(
                    skipped_types.items()))
        summary = ('Backup finished.\n\n'
                   'Exported: {}\nPartly exported (one format failed): {}\n'
                   'Failed: {}{}\n\nDetails: {}').format(
                       ok, partial, failed, skipped_txt,
                       os.path.join(root, LOG_NAME))
        log(summary.replace('\n', ' | '))
        ui.messageBox(summary, 'Fusion Backup')

    except Exception:
        log('CRASH: ' + traceback.format_exc())
        if ui:
            ui.messageBox('Backup stopped with an error:\n{}'.format(
                traceback.format_exc()), 'Fusion Backup')
    finally:
        if progress:
            try:
                progress.hide()
            except Exception:
                pass
        if _log_fh:
            _log_fh.close()
            _log_fh = None
